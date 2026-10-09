"""公式RPC contextと偽RTCを通すhost試験。実接続・実出音は検証しない。"""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.rtc.participant import PublishDataError

from local_gpt_live import livekit_input
from local_gpt_live.playback import AudioPacket
from local_gpt_live.session import Event

from test_livekit_input import Stream, Track, readiness_pending, statistics
from test_livekit_transport import FakeParticipant, Rig, eventually


class RpcParticipant(FakeParticipant):
    def __init__(self, rig):
        super().__init__(rig)
        self.handlers = {}
        self.notifications = []
        self.notification_error = None
        self.notification_attempts = 0

    def register_rpc_method(self, method_name, handler=None):
        def register(callback):
            self.handlers[method_name] = callback
            return callback
        return register(handler) if handler is not None else register

    def unregister_rpc_method(self, method):
        self.handlers.pop(method, None)

    async def publish_data(self, payload, *, reliable=True, destination_identities=None, topic=""):
        self.notification_attempts += 1
        if self.notification_error is not None:
            raise self.notification_error
        message = json.loads(payload)
        self.notifications.append((message, destination_identities, reliable, topic))


def assert_public_message(value):
    if isinstance(value, dict):
        assert not {"text", "pcm", "token", "exception"}.intersection(value)
        for item in value.values():
            assert_public_message(item)
    elif isinstance(value, list):
        for item in value:
            assert_public_message(item)
    elif isinstance(value, str):
        for marker in ("secret", "合成入力"):
            assert marker.casefold() not in value.casefold()


class HostRig:
    def __init__(self, monkeypatch):
        self.rig = Rig(monkeypatch)
        self.rig.auto_ready = False
        self.participant = RpcParticipant(self.rig)
        self.rig.room.local_participant = self.participant
        self.remote = SimpleNamespace(identity="fixture-user", sid="PA-fixture",
                                      track_publications={})
        self.rig.room.remote_participants[self.remote.identity] = self.remote
        Stream.instances = []
        monkeypatch.setattr(livekit_input.rtc, "AudioStream", Stream)
        self.track = self.add_track("TR-input")
        self.host = None
        self.calls = 0

    def add_track(self, sid):
        track = Track()
        track.sid = sid
        self.remote.track_publications[sid] = SimpleNamespace(
            sid=sid, track=track, subscribed=True, muted=False,
            kind=rtc.TrackKind.KIND_AUDIO, source=rtc.TrackSource.SOURCE_MICROPHONE,
        )
        return track

    async def start(self):
        module = importlib.import_module("local_gpt_live.livekit_host")
        self.host = module.LiveKitHost(self.rig.transport, session_id="session-fixture",
                                      clock_ns=lambda: self.rig.now_ns)
        await self.host.connect()
        self.control_method, self.ack_method = module.CONTROL_METHOD, module.ACK_METHOD

    def message(self, kind, **fields):
        return dict(v=1, type=kind, session_id="session-fixture", binding=self.host.binding,
                    request_id=f"request-{self.calls}", **fields)

    async def invoke(self, payload, *, identity="fixture-user", ack=False, timeout=5.0):
        self.calls += 1
        data = rtc.RpcInvocationData(request_id=f"rpc-{self.calls}", caller_identity=identity,
                                     payload=payload, response_timeout=timeout)
        handler = self.participant.handlers[self.ack_method if ack else self.control_method]
        async with asyncio.timeout(2):
            return json.loads(await handler(data))

    async def control(self, kind, **fields):
        return await self.invoke(json.dumps(self.message(kind, **fields)))

    async def prepare(self, *, track_sid="TR-input", request_id=None):
        message = self.message("open_input", track_sid=track_sid,
                               expected_revision=self.rig.audio.backend.revision)
        if request_id is not None:
            message["request_id"] = request_id
        result = await self.invoke(json.dumps(message))
        assert result["ok"]
        return result, message

    async def input_ack(self, result, **changes):
        message = self.message("input_ack", grant=result["grant"])
        message.update(changes)
        return await self.invoke(json.dumps(message))

    async def ready(self, response_id, track_sid, **changes):
        return await self.control("confirm_output_ready", response_id=response_id,
                                  track_sid=track_sid, **changes)

    async def response(self):
        result = await self.control("text", text="合成入力")
        assert result["ok"]
        response_id = result["response_id"]
        await eventually(lambda: any(p.response_id == response_id for p in self.rig.published))
        track = next(p for p in self.rig.published if p.response_id == response_id)
        assert (await self.ready(response_id, track.track_sid))["ok"]
        await eventually(lambda: any(s.response_id == response_id for s in self.rig.segments))
        return response_id, track.track_sid


async def request_state(h, *, identity="fixture-user", **fields):
    from local_gpt_live.livekit_host import STATE_METHOD
    payload = {"v": 1, "session_id": h.host.session_id, "connection_id": h.host.connection_id, **fields}
    data = rtc.RpcInvocationData(request_id="state-request", caller_identity=identity,
                                 payload=json.dumps(payload), response_timeout=5)
    return json.loads(await h.participant.handlers[STATE_METHOD](data))


async def test_state_handshake_recovers_missing_initial_notification_without_mutating_grant(h):
    await h.start()
    await h.host._notifications.join()
    initial = h.participant.notifications.pop()[0]
    prepared, _ = await h.prepare()
    binding, grant = h.host.binding, h.rig.audio.backend.grant
    result = await request_state(h)
    assert result["ok"] and result["type"] == "host_state"
    assert result["connection_id"] == initial["connection_id"]
    assert result["state_sequence"] > initial["state_sequence"]
    assert result["control_binding"] == binding
    assert result["input_revision"] == grant.input_revision
    assert h.rig.audio.backend.grant is grant
    assert Stream.instances == []
    assert (await h.input_ack(prepared))["ok"]


@pytest.mark.parametrize("fields,reason", [
    ({"identity": "other-user"}, "unauthorized"),
    ({"session_id": "old-session"}, "stale_binding"),
    ({"connection_id": "old-connection"}, "stale_binding"),
    ({"binding": "arbitrary"}, "invalid_control"),
])
async def test_state_handshake_rejects_wrong_caller_or_scope_without_disclosing_state(h, fields, reason):
    await h.start()
    revision, binding = h.rig.audio.backend.revision, h.host.binding
    assert await request_state(h, **fields) == {"ok": False, "reason": reason}
    assert h.rig.audio.backend.revision == revision
    assert h.host.binding == binding


async def test_state_handshake_rejects_same_identity_with_replaced_sid(h):
    await h.start()
    h.remote.sid = "PA-replaced"
    assert await request_state(h) == {"ok": False, "reason": "unauthorized"}


class WireClient:
    """認証済み接続の初期通知以降、RPC応答と通知だけから状態を更新する。"""

    def __init__(self, rig, initial):
        self.rig = rig
        self.connection_id = initial["connection_id"]
        self.state = initial
        self.calls = 0

    def accept(self, message):
        if (message.get("connection_id") == self.connection_id
                and message["state_sequence"] >= self.state["state_sequence"]):
            self.state = message

    async def control(self, kind, **fields):
        self.calls += 1
        result = await self.rig.invoke(json.dumps(dict(
            v=1, type=kind, session_id=self.state["session_id"],
            binding=self.state["control_binding"], request_id=f"wire-{self.calls}", **fields,
        )))
        self.accept(result)
        return result

    async def event(self, kind):
        await eventually(lambda: any(n[0]["type"] == kind
                                    for n in self.rig.participant.notifications))
        message = next(n[0] for n in reversed(self.rig.participant.notifications)
                       if n[0]["type"] == kind)
        self.accept(message)
        return message

    async def open(self, track_sid):
        prepared = await self.control("open_input", track_sid=track_sid,
                                      expected_revision=self.state["input_revision"])
        assert prepared["ok"]
        assert (await self.control("input_ack", grant=prepared["grant"]))["ok"]


async def wire_client(h):
    await h.start()
    await eventually(lambda: bool(h.participant.notifications))
    initial = h.participant.notifications[0][0]
    assert initial["type"] == "host_state"
    return WireClient(h, initial)


async def pending_output(h, monkeypatch):
    """入力準備の帰還より先に別callbackがSID喪失を検出しないようにする。"""
    await h.start()
    response_id, _ = await h.response()
    await h.host._notifications.join()
    notification = h.rig.gate(suppress_cancel=True)
    publish_data = h.participant.publish_data

    async def delayed_notification(*args, **kwargs):
        await notification.wait()
        await publish_data(*args, **kwargs)

    monkeypatch.setattr(h.participant, "publish_data", delayed_notification)
    h.rig.session.emit(Event("generation_completed", response_id))
    await notification.entered.wait()
    capture = h.rig.gate(suppress_cancel=True)
    h.rig.capture_gate = capture
    sequence = h.rig.session.playback.last_audio_sequence + 1
    assert h.rig.session.playback.enqueue(AudioPacket(response_id, sequence, h.rig.tts.wav))
    await capture.entered.wait()
    before = h.rig.transport.sent_audio_progress(response_id)
    assert before.submitted_sample_end == 400
    return response_id, sequence, capture, notification, before


async def assert_preparation_loss_stopped_output(h, output, result):
    response_id, sequence, capture, notification, before = output
    source, track = h.rig.sources[-1], h.rig.tracks[-1]
    assert result["closed"] and result["active_response_id"] is None
    assert not capture.release.is_set() and not notification.release.is_set()
    assert h.rig.session.active is None and h.rig.session.playback.active is None
    assert source.queue == [] and source.clear_calls > 0 and track.mute_calls > 0
    assert source.close_calls == 0
    frozen = h.rig.transport.sent_audio_progress(response_id)
    assert frozen.frozen_at_ns is not None
    assert frozen.submitted_sample_end == before.submitted_sample_end
    assert frozen.blocks == before.blocks
    assert h.rig.audio.backend.grant is None and h.host._input is None
    capture.release.set()
    notification.release.set()
    await eventually(lambda: bool(source.close_calls))
    assert h.rig.transport.sent_audio_progress(response_id) == frozen
    assert source.queue == []
    assert not any(s.sequence == sequence for s in h.rig.segments)
    assert not any(n[0]["type"] == "segment_sent" and n[0]["sequence"] == sequence
                   for n in h.participant.notifications)


@pytest.mark.parametrize("change", ["same", "sid", "missing", "muted"])
async def test_preparation_statistics_return_rechecks_authorization(h, monkeypatch, change):
    output = await pending_output(h, monkeypatch)
    response_id, sequence, capture, notification, before = output
    statistics_gate = h.rig.gate()
    get_stats = h.track.get_stats

    async def delayed_statistics():
        await statistics_gate.wait()
        return await get_stats()

    monkeypatch.setattr(h.track, "get_stats", delayed_statistics)
    revision = h.rig.audio.backend.revision
    generation = h.rig.session.generation
    message = h.message("open_input", track_sid="TR-input", expected_revision=revision)
    message["request_id"] = "q-open"
    opening = asyncio.create_task(h.invoke(json.dumps(message)))
    await statistics_gate.entered.wait()
    operation = h.host._input
    assert revision == 1 and h.rig.audio.backend.grant is None
    assert Stream.instances == [] and h.rig.pipeline.received == []
    assert h.rig.session.active == response_id
    if change == "sid":
        h.remote.sid = "PA-rejoined"
    elif change == "missing":
        h.rig.room.remote_participants.clear()
    elif change == "muted":
        h.remote.track_publications["TR-input"].muted = True
    statistics_gate.release.set()
    result = await opening
    assert_public_message(result)
    assert Stream.instances == [] and h.rig.pipeline.received == []
    if change in {"sid", "missing"}:
        assert not result["ok"] and result["reason"] == "operation_failed"
        assert h.rig.audio.backend.revision == revision
        assert operation.timer.cancelled()
        assert h.rig.session.generation == generation + 1
        await assert_preparation_loss_stopped_output(h, output, result)
        return
    if change == "muted":
        assert not result["ok"] and result["reason"] == "operation_failed"
        assert result["input_revision"] == revision and not result["input_active"]
        assert h.rig.audio.backend.grant is None and h.host._input is None
        assert operation.timer.cancelled()
    else:
        assert result["ok"] and result["grant"]["track_sid"] == "TR-input"
        assert result["input_active"] and not operation.started
    assert not result["closed"] and result["active_response_id"] == response_id
    assert h.rig.session.active == h.rig.session.playback.active == response_id
    assert h.rig.session.generation == generation
    assert h.rig.sources[-1].clear_calls == 0 and h.rig.tracks[-1].mute_calls == 0
    capture.release.set()
    notification.release.set()
    await eventually(lambda: any(s.sequence == sequence for s in h.rig.segments))
    assert h.rig.transport.sent_audio_progress(response_id).submitted_sample_end > before.submitted_sample_end


@pytest.mark.parametrize("changed_sid", [False, True])
async def test_preparation_cleanup_return_rechecks_authorization(h, monkeypatch, changed_sid):
    output = await pending_output(h, monkeypatch)
    reader_gate = h.rig.gate(suppress_cancel=True)

    class DelayedStream(Stream):
        async def __anext__(self):
            await reader_gate.wait()
            raise StopAsyncIteration

    monkeypatch.setattr(livekit_input.rtc, "AudioStream", DelayedStream)
    prepared, _ = await h.prepare()
    assert (await h.input_ack(prepared))["ok"]
    await reader_gate.entered.wait()
    assert (await h.control("mute", enabled=True))["ok"]
    await reader_gate.cancelled.wait()
    state = await h.control("mute", enabled=False)
    h.add_track("TR-after-cleanup")
    cleanup_entered = asyncio.Event()
    wait_for_cleanup = h.rig.transport.input.wait_for_cleanup

    async def observed_cleanup():
        cleanup_entered.set()
        await wait_for_cleanup()

    monkeypatch.setattr(h.rig.transport.input, "wait_for_cleanup", observed_cleanup)
    message = h.message("open_input", track_sid="TR-after-cleanup",
                        expected_revision=state["input_revision"])
    opening = asyncio.create_task(h.invoke(json.dumps(message)))
    await cleanup_entered.wait()
    assert not opening.done() and h.rig.audio.backend.grant is None
    assert len(Stream.instances) == 1 and h.rig.pipeline.received == []
    assert h.rig.audio.backend.revision == state["input_revision"]
    if changed_sid:
        h.remote.sid = "PA-rejoined"
    reader_gate.release.set()
    result = await opening
    assert Stream.instances[0].closed == 1
    assert len(Stream.instances) == 1 and h.rig.pipeline.received == []
    if changed_sid:
        assert not result["ok"] and result["reason"] == "operation_failed"
        assert h.rig.audio.backend.revision == state["input_revision"]
        await assert_preparation_loss_stopped_output(h, output, result)
    else:
        assert result["ok"] and result["grant"]["track_sid"] == "TR-after-cleanup"
        assert not result["closed"] and h.rig.session.active == output[0]
        output[2].release.set()
        output[3].release.set()
        await eventually(lambda: any(s.sequence == output[1] for s in h.rig.segments))


@pytest.mark.parametrize("changed_sid", [False, True])
async def test_preparation_reset_return_keeps_single_stop(h, monkeypatch, changed_sid):
    output = await pending_output(h, monkeypatch)
    reset_gate = h.rig.gate()
    worker = h.rig.audio.backend._worker
    reset = worker.reset

    async def delayed_reset(*args, **kwargs):
        await reset_gate.wait()
        await reset(*args, **kwargs)

    monkeypatch.setattr(worker, "reset", delayed_reset)
    generation = h.rig.session.generation
    opening = asyncio.create_task(h.control("open_input", track_sid="TR-input",
                                             expected_revision=h.rig.audio.backend.revision))
    await reset_gate.entered.wait()
    revision = h.rig.audio.backend.revision
    assert revision == 2 and h.rig.audio.backend.grant is None
    if changed_sid:
        h.remote.sid = "PA-rejoined"
    reset_gate.release.set()
    result = await opening
    if changed_sid:
        assert not result["ok"]
        assert h.rig.session.generation == generation + 1
        assert h.rig.audio.backend.revision > revision
        assert any(e.kind == "input_invalidated" for e in h.rig.events)
        await assert_preparation_loss_stopped_output(h, output, result)
    else:
        assert result["ok"] and h.rig.session.generation == generation
        assert (await h.input_ack(result))["ok"]
        for _ in range(10):
            Stream.instances[-1].push()
        await eventually(lambda: len(h.rig.pipeline.received) == 10)
        output[2].release.set()
        output[3].release.set()
        await eventually(lambda: any(s.sequence == output[1] for s in h.rig.segments))


@pytest.mark.parametrize("entry", ["control", "ack", "event", "notify"])
@pytest.mark.parametrize("loss", ["same", "sid", "missing"])
async def test_authorization_loss_stops_before_pending_capture_cleanup(h, monkeypatch, entry, loss):
    await h.start()
    response_id, sid = await h.response()
    await h.host._notifications.join()
    notification = h.rig.gate(suppress_cancel=True)
    publish_data = h.participant.publish_data

    async def delayed_notification(*args, **kwargs):
        await notification.wait()
        await publish_data(*args, **kwargs)

    monkeypatch.setattr(h.participant, "publish_data", delayed_notification)
    h.rig.session.emit(Event("generation_completed", response_id))
    await notification.entered.wait()
    pending_metadata_sequence = h.rig.session.playback.last_audio_sequence + 1
    if entry == "notify":
        # 実capture完了から生成したsegment_sentを、送信前のqueueへ保存する。
        assert h.rig.session.playback.enqueue(
            AudioPacket(response_id, pending_metadata_sequence, h.rig.tts.wav),
        )
        await eventually(lambda: any(s.sequence == pending_metadata_sequence
                                     for s in h.rig.segments))
    capture = h.rig.gate(suppress_cancel=True)
    h.rig.capture_gate = capture
    sequence = h.rig.session.playback.last_audio_sequence + 1
    assert h.rig.session.playback.enqueue(AudioPacket(response_id, sequence, h.rig.tts.wav))
    await capture.entered.wait()
    source, track = h.rig.sources[-1], h.rig.tracks[-1]
    before = h.rig.transport.sent_audio_progress(response_id)
    assert before.submitted_sample_end > 0 and source.queue
    ack_binding = h.host._output_binding
    if loss == "sid":
        h.remote.sid = "PA-rejoined"
    elif loss == "missing":
        h.rig.room.remote_participants.clear()
    if entry == "control":
        result = await h.ready(response_id, sid)
        assert result["ok"] if loss == "same" else result["reason"] == "unauthorized"
    elif entry == "ack":
        result = await h.invoke(json.dumps(dict(v=1, type="playback_ack", binding=ack_binding,
                                                response_id=response_id, audio_sequence=0)), ack=True)
        assert result["ok"] if loss == "same" else result["reason"] == "unauthorized"
    elif entry == "event":
        h.rig.session.emit(Event("generation_completed", response_id))
    else:
        attempts = h.participant.notification_attempts
        notification.release.set()
        if loss == "same":
            await eventually(lambda: h.participant.notification_attempts == attempts + 2)
        else:
            await eventually(lambda: h.host._closed)
            assert h.participant.notification_attempts == attempts + 1
            assert not any(n[0]["type"] == "segment_sent"
                           and n[0]["sequence"] == pending_metadata_sequence
                           for n in h.participant.notifications)
    if loss == "same":
        assert h.rig.session.active == response_id
        assert h.rig.session.playback.active == response_id
        assert source.clear_calls == 0 and track.mute_calls == 0
        capture.release.set()
        notification.release.set()
        await eventually(lambda: any(s.sequence == sequence for s in h.rig.segments))
        after = h.rig.transport.sent_audio_progress(response_id)
        assert after.submitted_sample_end > before.submitted_sample_end
        return
    # captureはまだ保留。aclose完了を待たず、同期的な停止を検査する。
    assert not capture.release.is_set()
    assert h.rig.session.active is None
    assert h.rig.session.playback.active is None
    assert h.rig.session.playback.confirmed_sequence == -1
    assert source.queue == [] and source.clear_calls > 0
    assert track.mute_calls > 0
    frozen = h.rig.transport.sent_audio_progress(response_id)
    assert frozen.frozen_at_ns is not None
    assert frozen.submitted_sample_end == before.submitted_sample_end
    capture.release.set()
    notification.release.set()
    await eventually(lambda: bool(source.close_calls))
    assert h.rig.transport.sent_audio_progress(response_id) == frozen
    assert source.queue == []
    assert not any(s.sequence == sequence for s in h.rig.segments)


async def test_track_callback_detects_sid_loss_and_reclaims_late_publish(h, monkeypatch):
    await h.start()
    await h.host._notifications.join()
    notification = h.rig.gate(suppress_cancel=True)

    async def delayed_notification(*args, **kwargs):
        await notification.wait()

    monkeypatch.setattr(h.participant, "publish_data", delayed_notification)
    publication = h.rig.gate(suppress_cancel=True)
    h.rig.publish_gate = publication
    result = await h.control("text", text="合成入力")
    await publication.entered.wait()
    await notification.entered.wait()
    h.remote.sid = "PA-rejoined"
    publication.release.set()
    await eventually(lambda: bool(h.rig.published))
    assert h.rig.session.active is None
    assert h.rig.session.playback.active is None
    assert h.rig.sources[-1].capture_calls == 0
    assert h.rig.sources[-1].clear_calls > 0 and h.rig.tracks[-1].mute_calls > 0
    assert not any(n[0]["type"] == "output_track" for n in h.participant.notifications)
    notification.release.set()
    await eventually(lambda: bool(h.participant.unpublished))
    assert h.participant.unpublished == [h.rig.published[-1].track_sid]
    assert h.rig.transport.sent_audio_progress(result["response_id"]).submitted_sample_end == 0


async def test_segment_callback_sid_loss_stops_following_segment(h, monkeypatch):
    await h.start()
    notification = h.rig.gate(suppress_cancel=True)

    async def delayed_notification(*args, **kwargs):
        await notification.wait()

    monkeypatch.setattr(h.participant, "publish_data", delayed_notification)
    capture = h.rig.gate(suppress_cancel=True)
    h.rig.capture_gate = capture
    result = await h.control("text", text="合成入力")
    response_id = result["response_id"]
    await eventually(lambda: bool(h.rig.published))
    assert (await h.ready(response_id, h.rig.published[-1].track_sid))["ok"]
    await capture.entered.wait()
    sequence = h.rig.session.playback.last_audio_sequence + 1
    assert h.rig.session.playback.enqueue(AudioPacket(response_id, sequence, h.rig.tts.wav))

    async def change_sid_at_last_frame(source, frame):
        if source.capture_calls == 3:
            h.remote.sid = "PA-rejoined"

    h.rig.capture_hook = change_sid_at_last_frame
    capture.release.set()
    await eventually(lambda: bool(h.rig.segments))
    assert h.rig.session.active is None
    assert h.rig.sources[-1].capture_calls == 3
    assert h.rig.sources[-1].queue == []
    assert h.rig.tracks[-1].mute_calls > 0
    snapshot = h.rig.transport.sent_audio_progress(response_id)
    assert snapshot.submitted_sample_end == 400 and snapshot.frozen_at_ns is not None
    assert len(h.rig.segments) == 1
    notification.release.set()


async def test_other_sender_rejection_keeps_authorized_output(h):
    await h.start()
    response_id, sid = await h.response()
    message = h.message("confirm_output_ready", response_id=response_id, track_sid=sid)
    result = await h.invoke(json.dumps(message), identity="other")
    assert result["reason"] == "unauthorized"
    assert h.rig.session.active == response_id and not h.host._closed
    assert h.rig.sources[-1].clear_calls == 0 and h.rig.tracks[-1].mute_calls == 0
    assert (await h.invoke(json.dumps(message)))["ok"]


async def test_sdk_disconnect_stops_once_before_async_cleanup(h):
    await h.start()
    response_id, _ = await h.response()
    generation = h.rig.session.generation
    h.rig.room.emit("disconnected")
    assert h.rig.session.active is None and h.rig.session.playback.active is None
    assert h.rig.session.generation == generation + 1
    assert h.rig.sources[-1].queue == [] and h.rig.tracks[-1].mute_calls > 0
    frozen = h.rig.transport.sent_audio_progress(response_id)
    revision = h.rig.audio.backend.revision
    h.rig.session.emit(Event("generation_completed", response_id))
    assert h.rig.session.generation == generation + 1
    assert h.rig.audio.backend.revision == revision
    assert h.rig.transport.sent_audio_progress(response_id) == frozen


async def test_late_reader_failure_cannot_invalidate_wire_reauthorized_input(h, monkeypatch):
    client = await wire_client(h)
    gate = h.rig.gate(suppress_cancel=True)

    class FailingStream(Stream):
        async def __anext__(self):
            await gate.wait()
            raise RuntimeError("synthetic-native-secret-reader")

    monkeypatch.setattr(livekit_input.rtc, "AudioStream", FailingStream)
    await client.open("TR-input")
    await gate.entered.wait()
    assert (await client.control("reconnect"))["ok"]
    before = h.rig.audio.backend.revision
    events = len(h.rig.events)
    h.add_track("TR-after-reader")
    pending = asyncio.create_task(client.control(
        "open_input", track_sid="TR-after-reader",
        expected_revision=client.state["input_revision"],
    ))
    await eventually(lambda: h.host._input is not None)
    assert not pending.done() and h.rig.audio.backend.grant is None
    assert h.rig.audio.backend.revision == before and len(Stream.instances) == 1
    monkeypatch.setattr(livekit_input.rtc, "AudioStream", Stream)
    gate.release.set()
    result = await pending
    assert result["ok"]
    assert result["input_revision"] == before + 1
    assert not any(e.kind == "input_rejected" for e in h.rig.events[events:])
    assert (await client.control("input_ack", grant=result["grant"]))["ok"]
    for _ in range(10):
        Stream.instances[-1].push()
    await eventually(lambda: len(h.rig.pipeline.received) == 10)


@pytest.mark.parametrize("reset_fails", [False, True])
async def test_pcm_failure_reset_outcome_notifies_and_allows_wire_recovery(h, monkeypatch, caplog,
                                                                        reset_fails):
    client = await wire_client(h)
    prepared = await client.control("open_input", track_sid="TR-input",
                                    expected_revision=client.state["input_revision"])
    assert prepared["ok"]
    assert (await client.control("input_ack", grant=prepared["grant"]))["ok"]
    grant = h.rig.audio.backend.grant
    feed = h.rig.pipeline.feed
    failed = False

    def fail_once(pcm, *, start_sample):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic-native-secret-pcm")
        return feed(pcm, start_sample=start_sample)

    def reset(next_sample=None, *, quarantine=False):
        if quarantine and reset_fails:
            raise RuntimeError("synthetic-native-secret-reset")

    monkeypatch.setattr(h.rig.pipeline, "feed", fail_once)
    monkeypatch.setattr(h.rig.pipeline, "reset", reset)
    for _ in range(10):
        Stream.instances[-1].push()
    if not reset_fails:
        await eventually(lambda: len(h.rig.pipeline.received) == 9)
        assert h.rig.audio.backend.grant is grant
        assert not any(n[0]["type"] == "input_invalidated" for n in h.participant.notifications)
        for _ in range(10):
            Stream.instances[-1].push()
        await eventually(lambda: len(h.rig.pipeline.received) == 19)
        return
    invalidated = await client.event("input_invalidated")
    rejected = await client.event("input_rejected")
    assert not invalidated["input_active"] and not rejected["input_active"]
    assert invalidated["input_revision"] == h.rig.audio.backend.revision
    assert invalidated["input_revision"] > prepared["input_revision"]
    assert rejected["reason"] == "input_rejected"
    assert h.host._input is None
    await eventually(lambda: not h.rig.transport.input._tasks)
    h.add_track("TR-after-failure")
    new = await client.control("open_input", track_sid="TR-after-failure",
                               expected_revision=client.state["input_revision"])
    assert new["ok"]
    current = h.rig.audio.backend.grant
    revision = h.rig.audio.backend.revision
    assert not (await client.control("input_ack", grant=prepared["grant"]))["ok"]
    assert h.rig.audio.backend.grant is current and h.rig.audio.backend.revision == revision
    assert (await client.control("input_ack", grant=new["grant"]))["ok"]
    for _ in range(10):
        Stream.instances[-1].push()
    await eventually(lambda: len(h.rig.pipeline.received) == 10)
    for message, destinations, _, _ in h.participant.notifications:
        assert destinations == ["fixture-user"]
        assert_public_message(message)
    assert "synthetic-native-secret" not in caplog.text


@pytest.mark.parametrize("gate", ["mute", "focus"])
async def test_enabled_gate_rejects_unused_track_without_consuming_it(h, gate):
    client = await wire_client(h)
    h.add_track("TR-unused")
    assert (await client.control(gate, enabled=True))["ok"]
    before = h.rig.audio.backend.revision
    streams, frames = len(Stream.instances), len(h.rig.pipeline.received)
    rejected = await client.control("open_input", track_sid="TR-unused",
                                    expected_revision=client.state["input_revision"])
    assert not rejected["ok"] and rejected["reason"] == "input_suppressed"
    assert rejected["input_revision"] == before == h.rig.audio.backend.revision
    assert h.rig.audio.backend.grant is None
    assert len(Stream.instances) == streams and len(h.rig.pipeline.received) == frames
    assert (await client.control(gate, enabled=False))["ok"]
    await client.open("TR-unused")
    for _ in range(10):
        Stream.instances[-1].push()
    await eventually(lambda: len(h.rig.pipeline.received) == frames + 10)


async def test_voice_origin_ready_and_cancel_use_only_wire_state(h):
    client = await wire_client(h)

    class Stt:
        async def transcribe(self, pcm):
            return "合成入力"

    h.rig.session.stt = Stt()
    assert h.rig.session.submit_audio(bytes(3200), generation=h.rig.session.generation,
                                      overlap=None)
    started = await client.event("response_started")
    track = await client.event("output_track")
    assert track["binding"] != track["control_binding"]
    assert track["response_id"] == started["response_id"]
    assert (await client.control("confirm_output_ready", response_id=track["response_id"],
                                 track_sid=track["track_sid"]))["ok"]
    await eventually(lambda: bool(h.rig.segments))
    assert (await client.control("cancel", response_id=track["response_id"]))["ok"]
    assert h.rig.session.active is None
    await client.open("TR-input")


@pytest.mark.parametrize("gate", ["mute", "focus"])
async def test_wire_revision_survives_gate_reconnect_and_input_end(h, gate):
    client = await wire_client(h)
    await client.open("TR-input")
    before = client.state["input_revision"]
    assert (await client.control(gate, enabled=True))["ok"]
    assert client.state["input_revision"] > before
    assert (await client.control(gate, enabled=False))["ok"]
    h.add_track("TR-after-gate")
    await client.open("TR-after-gate")
    assert (await client.control("reconnect"))["ok"]
    h.add_track("TR-after-reconnect")
    await client.open("TR-after-reconnect")
    h.participant.notifications.clear()
    Stream.instances[-1].queue.put(None)
    invalidated = await client.event("input_invalidated")
    assert not invalidated["input_active"]
    h.add_track("TR-after-end")
    await client.open("TR-after-end")


async def test_wire_failures_are_sanitized_and_allow_next_input(h):
    client = await wire_client(h)
    old = dict(client.state)

    class Core:
        async def stream(self, text):
            raise RuntimeError("synthetic-native-secret")
            yield ""

    h.rig.session.core = Core()
    assert (await client.control("text", text="合成入力"))["ok"]
    failed = await client.event("response_failed")
    assert failed["active_response_id"] is None
    assert_public_message(failed)
    h.rig.session.emit(Event("transport_failed", detail="synthetic-native-secret"))
    assert_public_message(await client.event("transport_failed"))
    current = dict(client.state)
    client.accept(old)
    client.accept({**current, "connection_id": "previous-connection", "state_sequence": 99999})
    assert client.state == current
    await client.open("TR-input")


async def test_wire_ack_timeout_notifies_revision_for_reauthorization(h, monkeypatch):
    module = importlib.import_module("local_gpt_live.livekit_host")
    monkeypatch.setattr(module, "INPUT_ACK_SECONDS", 0.03)
    client = await wire_client(h)
    prepared = await client.control("open_input", track_sid="TR-input",
                                    expected_revision=client.state["input_revision"])
    assert prepared["ok"]
    invalidated = await client.event("input_invalidated")
    assert invalidated["input_revision"] > prepared["input_revision"]
    assert not invalidated["input_active"]
    h.add_track("TR-after-timeout")
    await client.open("TR-after-timeout")


async def test_wire_stt_failure_notifies_without_secrets_and_can_reauthorize(h):
    client = await wire_client(h)

    class Stt:
        async def transcribe(self, pcm):
            raise RuntimeError("synthetic-native-secret")

    h.rig.session.stt = Stt()
    assert h.rig.session.submit_audio(bytes(3200), generation=h.rig.session.generation,
                                      overlap=None)
    assert_public_message(await client.event("input_failed"))
    await client.open("TR-input")


@pytest.fixture
async def h(monkeypatch):
    fixture = HostRig(monkeypatch)
    try:
        yield fixture
    finally:
        for gate in fixture.rig.gates:
            gate.release.set()
        if fixture.host is not None:
            await fixture.host.aclose()
        await fixture.rig.transport.aclose()


async def test_registered_rpc_prepares_input_and_only_ack_starts_pcm(h):
    await h.start()
    result, _ = await h.prepare()
    assert result["grant"]["track_sid"] == h.track.sid
    assert h.track.calls > 0
    assert h.rig.pipeline.received == []
    assert Stream.instances == []
    assert (await h.input_ack(result))["ok"]
    for _ in range(10):
        Stream.instances[-1].push()
    await eventually(lambda: len(h.rig.pipeline.received) == 10)
    assert [start for _, start in h.rig.pipeline.received] == list(range(0, 1600, 160))


@pytest.mark.parametrize("identity,sid,session", [
    ("", "PA-fixture", "session-fixture"),
    ("other", "PA-fixture", "session-fixture"),
    ("fixture-user", "PA-rejoined", "session-fixture"),
    ("fixture-user", "PA-fixture", "another-session"),
])
async def test_rpc_sender_room_sid_and_session_must_all_match(h, identity, sid, session):
    await h.start()
    h.remote.sid = sid
    message = h.message("open_input", track_sid=h.track.sid, expected_revision=0)
    message["session_id"] = session
    result = await h.invoke(json.dumps(message), identity=identity)
    assert not result["ok"]
    assert h.rig.audio.backend.revision == 0
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


@pytest.mark.parametrize("changes", [
    {"identity": "fixture-user"}, {"binding": "old-binding"},
    {"expected_revision": True}, {"expected_revision": 0.0},
    {"expected_revision": None}, {"expected_revision": 999},
    {"expected_revision": float("nan")}, {"track_sid": []},
    {"v": True}, {"unexpected": 1},
])
async def test_invalid_control_fields_cannot_change_backend_generation(h, changes):
    await h.start()
    message = h.message("open_input", track_sid=h.track.sid, expected_revision=0)
    message.update(changes)
    assert not (await h.invoke(json.dumps(message)))["ok"]
    assert h.rig.audio.backend.revision == 0
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


@pytest.mark.parametrize("wrapper", [
    lambda wire: "```json\n" + wire + "\n```",
    lambda wire: "~~~json\n" + wire + "\n~~~",
    lambda wire: "/* comment */" + wire,
    lambda wire: "[" + wire + "]",
    lambda wire: wire[:-1],
    lambda wire: wire.replace('"v": 1', '"v": 1, "v": 1'),
    lambda wire: "null",
    lambda wire: "false",
])
async def test_json_outside_single_object_is_rejected_without_input(h, wrapper):
    await h.start()
    payload = json.dumps(h.message("open_input", track_sid=h.track.sid, expected_revision=0))
    assert not (await h.invoke(wrapper(payload)))["ok"]
    assert h.rig.audio.backend.grant is None
    assert h.rig.audio.backend.revision == 0
    assert Stream.instances == []


async def test_text_containing_operation_name_is_only_text(h):
    await h.start()
    seen = []

    class Core:
        async def stream(self, text):
            seen.append(text)
            yield "合成応答。"

    h.rig.session.core = Core()
    text = '{"type":"open_input"}'
    assert (await h.control("text", text=text))["ok"]
    await h.rig.session.drain()
    assert seen == [text]
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


@pytest.mark.parametrize("field,value", [
    ("track_sid", "old-track"), ("request_id", "old-request"),
    ("input_generation", 999), ("input_revision", 999), ("input_revision", True),
])
async def test_input_ack_compares_saved_grant_without_reconstructing_it(h, field, value):
    await h.start()
    result, _ = await h.prepare()
    bad_grant = dict(result["grant"], **{field: value})
    assert not (await h.input_ack(result, grant=bad_grant))["ok"]
    assert h.rig.pipeline.received == []
    assert Stream.instances == []
    assert h.rig.audio.backend.grant is None


@pytest.mark.parametrize("elapsed_ns,accepted", [(4_999_999_999, True), (5_000_000_001, False)])
async def test_input_ack_uses_five_second_deadline(h, elapsed_ns, accepted):
    await h.start()
    result, _ = await h.prepare()
    h.rig.now_ns += elapsed_ns
    assert (await h.input_ack(result))["ok"] is accepted
    if accepted:
        for _ in range(10):
            Stream.instances[-1].push()
        await eventually(lambda: len(h.rig.pipeline.received) == 10)
    else:
        assert h.rig.audio.backend.grant is None
        assert Stream.instances == []
        assert h.rig.pipeline.received == []


async def test_retry_does_not_extend_input_deadline_or_reissue_grant(h):
    await h.start()
    result, message = await h.prepare(request_id="same-request")
    h.rig.now_ns += 4_000_000_000
    retry = await h.invoke(json.dumps(message))
    assert retry["ok"]
    assert retry["grant"] == result["grant"]
    h.rig.now_ns += 1_000_000_001
    assert not (await h.input_ack(retry))["ok"]
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


async def test_statistics_wait_consumes_same_input_ack_deadline(h):
    await h.start()

    async def slow_statistics():
        h.rig.now_ns += 4_500_000_000
        return statistics()

    h.track.get_stats = slow_statistics
    result, _ = await h.prepare()
    h.rig.now_ns += 500_000_001
    assert not (await h.input_ack(result))["ok"]
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


async def test_caller_response_budget_limits_input_authorization(h):
    await h.start()
    payload = json.dumps(h.message("open_input", track_sid=h.track.sid, expected_revision=0))
    result = await h.invoke(payload, timeout=0.25)
    assert result["ok"]
    h.rig.now_ns += 250_000_001
    assert not (await h.input_ack(result))["ok"]
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


@pytest.mark.parametrize("event", ["disconnected", "reconnecting"])
async def test_disconnect_revokes_prepared_grant_and_saved_rpc_handler(h, event):
    await h.start()
    result, _ = await h.prepare()
    handler = h.participant.handlers[h.control_method]
    h.rig.room.emit(event)
    data = rtc.RpcInvocationData("late", "fixture-user",
                                json.dumps(h.message("input_ack", grant=result["grant"])), 5)
    assert not json.loads(await handler(data))["ok"]
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []


@pytest.mark.parametrize("operation", ["reconnect", "close"])
async def test_stop_does_not_wait_for_cancel_resistant_statistics(h, operation):
    await h.start()
    gate = h.rig.gate(suppress_cancel=True)

    async def late_statistics():
        await gate.wait()
        return statistics()

    h.track.get_stats = late_statistics
    opening = asyncio.create_task(h.control("open_input", track_sid=h.track.sid,
                                             expected_revision=0))
    try:
        await asyncio.wait_for(gate.entered.wait(), 1)
        await h.control(operation)
        assert h.rig.audio.backend.grant is None
        gate.release.set()
        result = await opening
        assert not result["ok"]
        assert h.rig.audio.backend.grant is None
        assert Stream.instances == []
    finally:
        gate.release.set()
        await asyncio.gather(opening, return_exceptions=True)


@pytest.mark.parametrize("gate", ["mute", "focus"])
async def test_device_gate_stops_input_and_clear_requires_new_track(h, gate):
    await h.start()
    result, _ = await h.prepare()
    assert (await h.input_ack(result))["ok"]
    stream = Stream.instances[-1]
    for _ in range(10):
        stream.push()
    await eventually(lambda: len(h.rig.pipeline.received) == 10)
    assert (await h.control(gate, enabled=True))["ok"]
    assert h.rig.audio.backend.grant is None
    await eventually(lambda: stream.closed == 1)
    assert (await h.control(gate, enabled=False))["ok"]
    assert not (await h.input_ack(result))["ok"]
    assert len(h.rig.pipeline.received) == 10
    h.add_track("replacement")
    new, _ = await h.prepare(track_sid="replacement")
    assert (await h.input_ack(new))["ok"]
    for _ in range(10):
        Stream.instances[-1].push()
    await eventually(lambda: len(h.rig.pipeline.received) == 20)


async def test_text_invalidates_prepared_input_and_old_output_before_new_response(h):
    await h.start()
    old_response, _ = await h.response()
    old_source = h.rig.sources[0]
    prepared, _ = await h.prepare()
    new = await h.control("text", text="次の合成入力")
    assert new["ok"]
    assert new["response_id"] != old_response
    assert old_source.queue == []
    assert h.rig.audio.backend.grant is None
    assert not (await h.input_ack(prepared))["ok"]
    assert h.rig.session.active == new["response_id"]


async def test_ready_rpc_rejects_wrong_response_track_and_binding_before_capture(h):
    await h.start()
    result = await h.control("text", text="合成入力")
    response_id = result["response_id"]
    await eventually(lambda: len(h.rig.published) == 1)
    sid = h.rig.published[0].track_sid
    for response, track, binding in [("old", sid, h.host.binding),
                                     (response_id, "wrong", h.host.binding),
                                     (response_id, sid, "old-binding")]:
        payload = h.message("confirm_output_ready", response_id=response, track_sid=track)
        payload["binding"] = binding
        assert not (await h.invoke(json.dumps(payload)))["ok"]
        assert h.rig.sources[0].capture_calls == 0
    assert (await h.ready(response_id, sid))["ok"]
    await eventually(lambda: len(h.rig.segments) == 1)


async def test_old_cancel_and_timer_cannot_stop_next_response(h):
    await h.start()
    old_id, old_sid = await h.response()
    old_binding = h.host.binding
    old_output = h.rig.transport._output
    assert (await h.control("cancel", response_id=old_id))["ok"]
    assert h.rig.session.active is None
    assert h.rig.sources[0].queue == []
    new_id, new_sid = await h.response()
    assert new_id != old_id
    old_cancel = h.message("cancel", response_id=old_id)
    old_cancel["binding"] = old_binding
    assert not (await h.invoke(json.dumps(old_cancel)))["ok"]
    assert not (await h.ready(old_id, old_sid))["ok"]
    h.rig.transport._finish_estimated_output(old_output)
    h.rig.session._expire(old_id)
    assert h.rig.session.active == h.rig.session.playback.active == new_id
    assert h.rig.segments[-1].track_sid == new_sid
    assert h.rig.sources[-1].queue


async def test_ready_rpc_after_deadline_cannot_start_capture(h):
    await h.start()
    result = await h.control("text", text="合成入力")
    response_id = result["response_id"]
    await eventually(lambda: len(h.rig.published) == 1)
    h.rig.now_ns += 200_000_001
    assert not (await h.ready(response_id, h.rig.published[0].track_sid))["ok"]
    assert h.rig.sources[0].capture_calls == 0
    assert h.rig.segments == []


async def test_current_ack_is_registered_and_does_not_imply_completion(h):
    await h.start()
    response_id, sid = await h.response()
    await eventually(lambda: any(n[0].get("sample_end") == 400 for n in h.participant.notifications))
    metadata = next(n[0] for n in h.participant.notifications if n[0].get("sample_end") == 400)
    assert metadata["response_id"] == response_id
    assert metadata["track_sid"] == sid
    payload = dict(v=1, type="playback_ack", binding=metadata["binding"],
                   response_id=response_id, audio_sequence=0)
    assert (await h.invoke(json.dumps(payload), ack=True))["ok"]
    assert h.rig.session.playback.confirmed_sequence == 0
    assert h.rig.session.active == response_id
    assert not any(e.kind == "playback_completed" for e in h.rig.events)


async def test_notifications_target_authorized_participant_and_keep_estimate_meaning(h, caplog):
    await h.start()
    response_id, _ = await h.response()
    output = h.rig.transport._output
    h.rig.now_ns = output.progress.next_estimated_complete_at_ns()
    h.rig.transport._finish_estimated_output(output)
    await eventually(lambda: any(n[0].get("type") == "output_estimated_completed"
                                for n in h.participant.notifications))
    for message, destinations, _, _ in h.participant.notifications:
        assert destinations == [h.remote.identity]
        assert_public_message(message)
    estimate = next(n[0] for n in h.participant.notifications
                    if n[0].get("type") == "output_estimated_completed")
    assert estimate["response_id"] == response_id
    assert estimate["basis"] == "sdk_submitted_elapsed"
    assert estimate["real_playback_confirmed"] is False
    assert h.rig.session.playback.confirmed_sequence == -1
    assert not any(e.kind == "playback_completed" for e in h.rig.events)
    assert "secret" not in caplog.text.casefold()


@pytest.mark.parametrize("change", ["sid", "binding"])
async def test_queued_notifications_are_discarded_after_scope_changes(h, change):
    await h.start()
    gate = h.rig.gate()
    original = h.participant.publish_data
    calls = 0

    async def delayed(payload, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
        await original(payload, **kwargs)

    h.participant.publish_data = delayed
    result = await h.control("text", text="合成入力")
    await gate.entered.wait()
    await eventually(lambda: len(h.rig.published) == 1)
    response_id = result["response_id"]
    sid = h.rig.published[0].track_sid
    assert (await h.ready(response_id, sid))["ok"]
    await eventually(lambda: len(h.rig.segments) == 1)
    if change == "sid":
        h.remote.sid = "PA-rejoined"
    else:
        assert (await h.control("reconnect"))["ok"]
    gate.release.set()
    await eventually(lambda: h.host._notifications.empty())
    await asyncio.sleep(0)
    assert not any(n[0].get("type") == "segment_sent" for n in h.participant.notifications)


async def test_backend_reset_completion_after_reconnect_cannot_restore_old_input(h):
    await h.start()
    gate = h.rig.gate(suppress_cancel=True)
    worker = h.rig.audio.backend._worker
    original = worker.reset
    first = True

    async def delayed_reset(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            await gate.wait()
        await original(*args, **kwargs)

    worker.reset = delayed_reset
    opening = asyncio.create_task(h.control("open_input", track_sid=h.track.sid,
                                             expected_revision=0))
    try:
        await gate.entered.wait()
        assert (await h.control("reconnect"))["ok"]
        h.add_track("replacement")
        new_opening = asyncio.create_task(h.prepare(track_sid="replacement"))
        gate.release.set()
        assert not (await opening)["ok"]
        new, _ = await new_opening
        assert (await h.input_ack(new))["ok"]
        for _ in range(10):
            Stream.instances[-1].push()
        await eventually(lambda: len(h.rig.pipeline.received) == 10)
        assert h.rig.audio.backend.grant.track_sid == "replacement"
    finally:
        gate.release.set()
        await asyncio.gather(opening, return_exceptions=True)


async def test_notification_failure_does_not_generate_ready_or_publish_native_error(h, caplog):
    await h.start()
    h.participant.notification_error = PublishDataError("synthetic-native-secret")
    result = await h.control("text", text="合成入力")
    await eventually(lambda: len(h.rig.published) == 1)
    await eventually(lambda: h.participant.notification_attempts > 0)
    assert h.rig.sources[0].capture_calls == 0
    assert_public_message(result)
    assert "secret" not in caplog.text.casefold()
    assert h.rig.session.playback.confirmed_sequence == -1


async def test_conflicting_retry_cannot_switch_track_or_backend_revision(h):
    await h.start()
    result, message = await h.prepare(request_id="same-request")
    revision = h.rig.audio.backend.revision
    h.add_track("other-track")
    message["track_sid"] = "other-track"
    assert not (await h.invoke(json.dumps(message)))["ok"]
    assert h.rig.audio.backend.revision == revision
    grant = h.rig.audio.backend.grant
    assert grant is None or grant.track_sid == result["grant"]["track_sid"]
    assert Stream.instances == []


async def test_reconnect_requires_new_track_and_late_ack_cannot_revoke_new_grant(h):
    await h.start()
    old, _ = await h.prepare()
    assert (await h.input_ack(old))["ok"]
    old_stream = Stream.instances[-1]
    old_id, old_sid = await h.response()
    assert (await h.control("reconnect"))["ok"]
    assert h.rig.session.active is None
    assert h.rig.audio.backend.grant is None
    await eventually(lambda: old_stream.closed == 1)
    assert not (await h.control("open_input", track_sid=h.track.sid,
                               expected_revision=h.rig.audio.backend.revision))["ok"]
    assert not (await h.ready(old_id, old_sid))["ok"]
    h.add_track("new-track")
    new, _ = await h.prepare(track_sid="new-track")
    assert not (await h.input_ack(old))["ok"]
    assert (await h.input_ack(new))["ok"]
    for _ in range(10):
        Stream.instances[-1].push()
    await eventually(lambda: len(h.rig.pipeline.received) == 10)
    assert h.rig.audio.backend.grant.track_sid == "new-track"


@pytest.mark.parametrize("operation", ["cancel", "reconnect", "close"])
async def test_duplicate_stop_is_idempotent_or_explicitly_rejected(h, operation):
    await h.start()
    response_id, _ = await h.response()
    fields = {"response_id": response_id} if operation == "cancel" else {}
    payload = json.dumps(h.message(operation, **fields))
    handler = h.participant.handlers[h.control_method]
    first = await handler(rtc.RpcInvocationData("first", "fixture-user", payload, 5))
    assert json.loads(first)["ok"]
    generation = h.rig.session.generation
    second = json.loads(await handler(rtc.RpcInvocationData("duplicate", "fixture-user", payload, 5)))
    assert type(second["ok"]) is bool
    assert h.rig.session.generation == generation
    assert h.rig.session.active is None
    assert h.rig.sources[0].queue == []


async def test_missing_ack_revokes_partial_authorization_without_another_rpc(h):
    await h.start()
    await h.prepare()
    h.rig.now_ns += 5_000_000_001
    async with asyncio.timeout(6):
        while h.rig.audio.backend.grant is not None:
            await asyncio.sleep(0.001)
    assert h.rig.pipeline.received == []
    assert Stream.instances == []


async def test_oversized_control_payload_is_rejected_before_side_effects(h):
    await h.start()
    payload = json.dumps(h.message("text", text="x" * 1_000_000))
    assert not (await h.invoke(payload))["ok"]
    assert h.rig.session.active is None
    assert h.rig.audio.backend.revision == 0
    assert h.rig.sources == []


async def test_participant_sid_change_suppresses_notifications_and_controls(h):
    await h.start()
    await h.response()
    before = len(h.participant.notifications)
    h.remote.sid = "PA-rejoined"
    assert not (await h.control("text", text="次の合成入力"))["ok"]
    await h.host.aclose()
    assert len(h.participant.notifications) == before
    assert h.rig.audio.backend.grant is None


async def test_cancel_notification_contains_frozen_estimate_without_playback_ack(h):
    await h.start()
    response_id, _ = await h.response()
    assert (await h.control("cancel", response_id=response_id))["ok"]
    await eventually(lambda: any(n[0].get("type") == "output_estimated_stopped"
                                for n in h.participant.notifications))
    stopped = next(n for n in h.participant.notifications
                   if n[0].get("type") == "output_estimated_stopped")
    assert stopped[1] == [h.remote.identity]
    assert stopped[0]["response_id"] == response_id
    assert stopped[0]["basis"] == "sdk_submitted_elapsed"
    assert stopped[0]["real_playback_confirmed"] is False
    assert h.rig.session.playback.confirmed_sequence == -1


async def test_stream_start_failure_revokes_grant_and_sanitizes_rpc_result(h, monkeypatch, caplog):
    await h.start()
    prepared, _ = await h.prepare()

    def failed_stream(*args, **kwargs):
        raise RuntimeError("synthetic-native-secret")

    monkeypatch.setattr(livekit_input.rtc, "AudioStream", failed_stream)
    result = await h.input_ack(prepared)
    assert not result["ok"]
    assert h.rig.audio.backend.grant is None
    assert h.rig.pipeline.received == []
    assert_public_message(result)
    assert "secret" not in caplog.text.casefold()


async def test_stream_setup_crossing_input_deadline_cannot_start_pcm(h, monkeypatch):
    await h.start()
    prepared, _ = await h.prepare()
    original = livekit_input.rtc.AudioStream

    def slow_stream(*args, **kwargs):
        stream = original(*args, **kwargs)
        h.rig.now_ns += 5_000_000_001
        return stream

    monkeypatch.setattr(livekit_input.rtc, "AudioStream", slow_stream)
    assert not (await h.input_ack(prepared))["ok"]
    assert h.rig.audio.backend.grant is None
    assert h.rig.pipeline.received == []
    await eventually(lambda: Stream.instances[-1].closed == 1)


@pytest.mark.parametrize("kind", ["mute", "focus"])
async def test_same_gate_retry_does_not_advance_backend_revision(h, kind):
    await h.start()
    prepared, _ = await h.prepare()
    assert (await h.input_ack(prepared))["ok"]
    message = json.dumps(h.message(kind, enabled=True))
    first = await h.invoke(message)
    revision = h.rig.audio.backend.revision
    retry = await h.invoke(message)
    assert first["ok"] and retry["ok"]
    assert retry["input_revision"] == revision == h.rig.audio.backend.revision


async def test_partial_rpc_registration_failure_reclaims_handlers_and_transport(h):
    original = h.participant.register_rpc_method

    def fails_on_ack(method, handler=None):
        result = original(method, handler)
        if "playback-ack" in method:
            raise RuntimeError("synthetic-native-secret")
        return result

    h.participant.register_rpc_method = fails_on_ack
    with pytest.raises(RuntimeError, match="^host_connection_failed$"):
        await h.start()
    assert h.participant.handlers == {}
    assert not h.rig.room.isconnected()
    assert h.rig.audio.backend.grant is None


@pytest.mark.parametrize("malformed", [None, [], {"unexpected": 1}])
async def test_malformed_input_ack_revokes_current_partial_grant(h, malformed):
    await h.start()
    result, _ = await h.prepare()
    assert not (await h.input_ack(result, grant=malformed))["ok"]
    assert h.rig.audio.backend.grant is None
    assert Stream.instances == []
    assert h.rig.pipeline.received == []


async def test_rpc_delayed_subscription_reuses_pending_request_and_ack_budget(h):
    await h.start()
    publication = h.remote.track_publications[h.track.sid]
    publication.subscribed = False
    baseline = {event: tuple(handlers) for event, handlers in h.rig.room.listeners.items()}
    message = h.message("open_input", track_sid=h.track.sid, expected_revision=0)
    opening = asyncio.create_task(h.invoke(json.dumps(message)))
    retry = None
    try:
        await readiness_pending(h.rig.room, opening)
        assert h.rig.audio.backend.grant is None
        assert Stream.instances == []
        h.rig.now_ns += 3_000_000_000
        retry = asyncio.create_task(h.invoke(json.dumps(message)))
        h.add_track("unrelated")
        conflict = await h.invoke(json.dumps({**message, "track_sid": "unrelated"}))
        assert not conflict["ok"]
        publication.subscribed = True

        async def statistics_after_subscription():
            h.rig.now_ns += 1_500_000_000
            return statistics()

        h.track.get_stats = statistics_after_subscription
        h.rig.room.emit("track_subscribed", h.track, publication, h.remote)
        result, duplicate = await asyncio.gather(opening, retry)
        assert result["ok"] and duplicate["ok"]
        assert result["grant"] == duplicate["grant"]
        assert 0 < result["remaining_ms"] <= 500
        assert Stream.instances == []
        assert h.rig.pipeline.received == []
        assert {e: tuple(v) for e, v in h.rig.room.listeners.items() if v} == {
            e: v for e, v in baseline.items() if v}
        h.rig.now_ns += 500_000_001
        assert not (await h.input_ack(result))["ok"]
        assert h.rig.audio.backend.grant is None
        assert Stream.instances == []
    finally:
        opening.cancel()
        if retry is not None:
            retry.cancel()
        await asyncio.gather(opening, *([] if retry is None else [retry]), return_exceptions=True)


@pytest.mark.parametrize("ready", [False, True])
async def test_rpc_short_budget_expires_during_subscription_or_statistics(h, ready):
    await h.start()
    publication = h.remote.track_publications[h.track.sid]
    publication.subscribed = False
    message = h.message("open_input", track_sid=h.track.sid, expected_revision=0)
    opening = asyncio.create_task(h.invoke(json.dumps(message), timeout=0.08))
    gate = h.rig.gate(suppress_cancel=True)

    async def delayed_stats():
        await gate.wait()
        return statistics()

    h.track.get_stats = delayed_stats
    try:
        await readiness_pending(h.rig.room, opening)
        if ready:
            publication.subscribed = True
            h.rig.room.emit("track_subscribed", h.track, publication, h.remote)
            await asyncio.wait_for(gate.entered.wait(), 1)
        result = await asyncio.wait_for(opening, 1)
        assert not result["ok"]
        gate.release.set()
        publication.subscribed = True
        h.rig.room.emit("track_subscribed", h.track, publication, h.remote)
        await h.rig.transport.input.wait_for_cleanup()
        assert h.rig.audio.backend.grant is None
        assert h.rig.pipeline.received == []
        assert Stream.instances == []
    finally:
        gate.release.set()
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)


async def test_rpc_cleanup_wait_consumes_original_preparation_deadline(h):
    await h.start()
    old_gate = h.rig.gate(suppress_cancel=True)

    async def old_stats():
        await old_gate.wait()
        return statistics()

    h.track.get_stats = old_stats
    old = asyncio.create_task(h.control("open_input", track_sid=h.track.sid,
                                        expected_revision=0))
    new = None
    try:
        await old_gate.entered.wait()
        assert (await h.control("mute", enabled=True))["ok"]
        assert not (await old)["ok"]
        assert (await h.control("mute", enabled=False))["ok"]
        replacement = h.add_track("replacement")
        message = h.message("open_input", track_sid=replacement.sid,
                            expected_revision=h.rig.audio.backend.revision)
        new = asyncio.create_task(h.invoke(json.dumps(message), timeout=0.03))
        # cleanupが帰還しない場合にも、新openの元予算で終了する。
        result = await asyncio.wait_for(new, 1)
        assert not result["ok"]
        assert replacement.calls == 0
        old_gate.release.set()
        await h.rig.transport.input.wait_for_cleanup()
        assert h.rig.audio.backend.grant is None
        assert Stream.instances == []
        assert h.rig.pipeline.received == []
        prepared, _ = await h.prepare(track_sid=replacement.sid)
        assert (await h.input_ack(prepared))["ok"]
        for _ in range(10):
            Stream.instances[-1].push()
        await eventually(lambda: len(h.rig.pipeline.received) == 10)
    finally:
        old_gate.release.set()
        old.cancel()
        if new is not None:
            new.cancel()
        await asyncio.gather(old, *([] if new is None else [new]), return_exceptions=True)


@pytest.mark.parametrize("operation", ["mute", "focus", "text", "cancel", "reconnect", "close",
                                      "disconnected", "reconnecting", "participant_disconnected"])
async def test_rpc_stop_while_subscription_pending_blocks_late_track(h, operation):
    await h.start()
    response_id = None
    if operation == "cancel":
        response_id, _ = await h.response()
    publication = h.remote.track_publications[h.track.sid]
    publication.subscribed = False
    baseline = {e: tuple(v) for e, v in h.rig.room.listeners.items() if v}
    opening = asyncio.create_task(h.control("open_input", track_sid=h.track.sid,
                                             expected_revision=h.rig.audio.backend.revision))
    try:
        await readiness_pending(h.rig.room, opening)
        if operation in {"disconnected", "reconnecting"}:
            h.rig.room.emit(operation)
        elif operation == "participant_disconnected":
            h.rig.room.remote_participants.clear()
            h.rig.room.emit(operation, h.remote)
        else:
            fields = {"enabled": True} if operation in {"mute", "focus"} else {}
            if operation == "text":
                fields["text"] = "合成入力"
            if operation == "cancel":
                fields["response_id"] = response_id
            assert (await h.control(operation, **fields))["ok"]
        result = await asyncio.wait_for(opening, 1)
        assert not result["ok"]
        publication.subscribed = True
        h.rig.room.emit("track_subscribed", h.track, publication, h.remote)
        assert h.rig.audio.backend.grant is None
        assert Stream.instances == []
        assert h.rig.pipeline.received == []
        if operation in {"mute", "focus", "text", "cancel", "reconnect"}:
            assert {e: tuple(v) for e, v in h.rig.room.listeners.items() if v} == baseline
            if operation in {"mute", "focus"}:
                assert (await h.control(operation, enabled=False))["ok"]
            h.add_track("replacement")
            new, _ = await h.prepare(track_sid="replacement")
            h.rig.room.emit("track_subscribed", h.track, publication, h.remote)
            assert (await h.input_ack(new))["ok"]
            for _ in range(10):
                Stream.instances[-1].push()
            await eventually(lambda: len(h.rig.pipeline.received) == 10)
            assert h.rig.audio.backend.grant.track_sid == "replacement"
    finally:
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
