"""実接続なしで、会話取消と RTC 所有資源の境界を合成検証する。"""

import asyncio
import io
import wave
from collections import defaultdict
from types import SimpleNamespace

import pytest

from local_gpt_live import livekit_transport
from local_gpt_live.demo import FixtureCore, FixtureStt
from local_gpt_live.input import AudioInput
from local_gpt_live.session import VoiceSession


def wav_bytes(*, rate=16000, channels=1, frames=400):
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * frames * channels)
    return data.getvalue()


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class Gate:
    """SDK/provider の遅着を、時間待ちに依存せず再現する。"""

    def __init__(self, *, suppress_cancel=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.suppress_cancel = suppress_cancel

    async def wait(self):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if not self.suppress_cancel:
                raise
            await self.release.wait()


class Pipeline:
    """モデルを起動せず、実 AudioInput/worker の解放だけを通す。"""

    def __init__(self):
        self.close_calls = 0

    def reset(self, next_sample=None, *, quarantine=False):
        pass

    def feed(self, pcm, *, start_sample):
        return ()

    def close(self):
        self.close_calls += 1


class Tts:
    def __init__(self):
        self.wav = wav_bytes()

    async def synthesize(self, text):
        return self.wav


class Rig:
    def __init__(self, monkeypatch):
        self.events = []
        self.sources = []
        self.tracks = []
        self.segments = []
        self.published = []
        self.gates = []
        self.auto_ready = True
        self.connect_gate = self.publish_gate = self.capture_gate = None
        self.connect_error = None
        self.pipeline = Pipeline()
        self.tts = Tts()
        self.session = VoiceSession(
            FixtureStt(), FixtureCore(), self.tts, livekit_transport.LiveKitPlayback(),
            emit=self.events.append,
        )
        self.audio = AudioInput(self.session, self.pipeline)
        self.room = FakeRoom(self)
        monkeypatch.setattr(livekit_transport.rtc, "Room", lambda: self.room)
        monkeypatch.setattr(livekit_transport.rtc, "AudioSource", self.new_source)
        monkeypatch.setattr(livekit_transport.rtc.LocalAudioTrack, "create_audio_track",
                            self.new_track)
        config = livekit_transport.LiveKitConfig(
            "wss://fixture.invalid", "synthetic-secret", "fixture-user", "PA-fixture",
            connect_timeout=0.2, close_timeout=0.05, output_ready_timeout=0.2,
        )
        self.transport = livekit_transport.LiveKitTransport(
            self.audio, config, on_segment=self.segments.append, on_track=self.on_track,
        )

    def gate(self, *, suppress_cancel=False):
        gate = Gate(suppress_cancel=suppress_cancel)
        self.gates.append(gate)
        return gate

    def new_source(self, rate, channels, *, queue_size_ms):
        source = FakeSource(self, rate, channels)
        self.sources.append(source)
        return source

    def new_track(self, name, source):
        track = FakeTrack(name, source)
        self.tracks.append(track)
        return track

    def on_track(self, published):
        self.published.append(published)
        if self.auto_ready:
            self.transport.confirm_output_ready(published.response_id, published.track_sid)


class FakeRoom:
    def __init__(self, rig):
        self.rig = rig
        self.listeners = defaultdict(list)
        self.remote_participants = {}
        self.local_participant = FakeParticipant(rig)
        self.connected = False
        self.connect_calls = self.disconnect_calls = 0
        self.connect_completed = asyncio.Event()

    def on(self, event, handler):
        self.listeners[event].append(handler)

    def off(self, event, handler):
        self.listeners[event].remove(handler)

    def emit(self, event, *args):
        if event == "disconnected":
            self.connected = False
        for handler in tuple(self.listeners[event]):
            handler(*args)

    def isconnected(self):
        return self.connected

    async def connect(self, url, token, options):
        self.connect_calls += 1
        if self.rig.connect_gate is not None:
            await self.rig.connect_gate.wait()
        if self.rig.connect_error is not None:
            raise self.rig.connect_error
        self.connected = True
        self.connect_completed.set()

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


class FakeParticipant:
    def __init__(self, rig):
        self.rig = rig
        self.publications = []
        self.unpublished = []

    async def publish_track(self, track, options):
        if self.rig.publish_gate is not None:
            await self.rig.publish_gate.wait()
        publication = SimpleNamespace(sid=f"TR-output-{len(self.publications) + 1}")
        self.publications.append(publication)
        return publication

    async def unpublish_track(self, sid):
        self.unpublished.append(sid)


class FakeSource:
    def __init__(self, rig, rate, channels):
        self.rig = rig
        self.rate, self.channels = rate, channels
        self.frames = []
        self.queue = []
        self.capture_calls = self.clear_calls = self.close_calls = 0

    async def capture_frame(self, frame):
        self.capture_calls += 1
        if self.rig.capture_gate is not None:
            gate, self.rig.capture_gate = self.rig.capture_gate, None
            await gate.wait()
        self.frames.append(frame)
        self.queue.append(frame)

    def clear_queue(self):
        self.clear_calls += 1
        self.queue.clear()

    async def aclose(self):
        self.close_calls += 1


class FakeTrack:
    def __init__(self, name, source):
        self.name, self.source = name, source
        self.mute_calls = 0

    def mute(self):
        self.mute_calls += 1


@pytest.fixture
async def rig(monkeypatch):
    fixture = Rig(monkeypatch)
    try:
        yield fixture
    finally:
        for gate in fixture.gates:
            gate.release.set()
        await fixture.transport.aclose()


async def test_wav_delivery_tracks_response_samples_without_playback_ack(rig):
    await rig.transport.connect()
    response_id = rig.session.submit_text("合成入力")
    await rig.session.drain()
    await eventually(lambda: len(rig.segments) == 1)

    source, track = rig.sources[0], rig.tracks[0]
    assert track.name == "ds-response-v1:" + response_id
    assert (source.rate, source.channels) == (16000, 1)
    assert b"".join(bytes(frame.data) for frame in source.frames) == b"\x01\x00" * 400
    assert [frame.samples_per_channel for frame in source.frames] == [160, 160, 80]
    segment = rig.segments[0]
    assert (segment.response_id, segment.sequence, segment.track_sid) == (
        response_id, 0, rig.published[0].track_sid,
    )
    assert (segment.sample_rate, segment.sample_start, segment.sample_end) == (16000, 0, 400)
    assert rig.session.active == rig.session.generated == response_id
    assert rig.session.playback.pending_bytes == 0
    assert "generation_completed" in [event.kind for event in rig.events]
    assert "playback_completed" not in [event.kind for event in rig.events]


async def test_segments_share_one_response_track_with_contiguous_sample_ranges(rig):
    class Core:
        async def stream(self, text):
            yield "一。二。"

    rig.session.core = Core()
    await rig.transport.connect()
    response_id = rig.session.submit_text("合成入力")
    await eventually(lambda: len(rig.segments) == 2)

    assert len(rig.sources) == len(rig.tracks) == len(rig.published) == 1
    assert [(s.sequence, s.sample_start, s.sample_end) for s in rig.segments] == [
        (0, 0, 400), (1, 400, 800),
    ]
    assert {s.track_sid for s in rig.segments} == {rig.published[0].track_sid}
    assert {s.response_id for s in rig.segments} == {response_id}


async def test_readiness_requires_current_response_and_exact_track_before_first_frame(rig):
    rig.auto_ready = False
    await rig.transport.connect()
    response_id = rig.session.submit_text("合成入力")
    await eventually(lambda: len(rig.published) == 1)
    sid = rig.published[0].track_sid

    assert rig.sources[0].frames == []
    assert not rig.transport.confirm_output_ready("another-response", sid)
    assert not rig.transport.confirm_output_ready(response_id, "another-track")
    assert rig.sources[0].frames == []
    assert rig.transport.confirm_output_ready(response_id, sid)
    await eventually(lambda: len(rig.segments) == 1)
    rig.transport.cancel()
    assert not rig.transport.confirm_output_ready(response_id, sid)


async def test_readiness_timeout_fails_closed_and_releases_published_track(rig):
    rig.auto_ready = False
    await rig.transport.connect()
    rig.session.submit_text("合成入力")
    await eventually(lambda: any(event.kind == "transport_failed" for event in rig.events))
    await rig.transport.aclose()

    assert rig.sources[0].frames == []
    assert rig.sources[0].close_calls == 1
    assert rig.room.local_participant.unpublished == [rig.published[0].track_sid]
    assert rig.session.active is None
    assert not rig.room.connected


async def test_cancel_clears_queue_again_when_native_capture_finishes_late(rig):
    gate = rig.capture_gate = rig.gate(suppress_cancel=True)
    await rig.transport.connect()
    old_id = rig.session.submit_text("旧応答")
    await gate.entered.wait()
    old_source, old_track = rig.sources[0], rig.tracks[0]

    rig.transport.cancel()
    assert rig.session.active is None
    assert old_source.clear_calls >= 1
    assert old_track.mute_calls >= 1
    gate.release.set()
    await eventually(lambda: old_source.close_calls == 1)

    assert old_source.capture_calls == 1
    assert old_source.queue == []
    assert old_source.clear_calls >= 2
    assert not gate.cancelled.is_set()
    assert rig.segments == []
    new_id = rig.session.submit_text("新応答")
    await eventually(lambda: len(rig.segments) == 1)
    assert new_id != old_id
    assert rig.segments[0].response_id == new_id
    assert rig.sources[1] is not old_source


async def test_cancel_during_late_publish_mutes_and_unpublishes_without_capture(rig):
    gate = rig.publish_gate = rig.gate(suppress_cancel=True)
    await rig.transport.connect()
    rig.session.submit_text("旧応答")
    await gate.entered.wait()
    rig.transport.cancel()
    assert rig.tracks[0].mute_calls >= 1
    gate.release.set()
    await eventually(lambda: rig.sources[0].close_calls == 1)

    assert rig.sources[0].capture_calls == 0
    assert rig.sources[0].queue == []
    assert rig.room.local_participant.unpublished == ["TR-output-1"]
    assert rig.published == rig.segments == []
    assert not gate.cancelled.is_set()


@pytest.mark.parametrize("operation", ["capture", "publish"])
async def test_close_is_bounded_and_reclaims_native_output_after_disconnect(rig, operation):
    gate = rig.gate()
    setattr(rig, operation + "_gate", gate)
    await rig.transport.connect()
    rig.session.submit_text("旧応答")
    await gate.entered.wait()

    async with asyncio.timeout(1):
        await rig.transport.aclose()
    assert rig.session.active is None
    assert rig.tracks[0].mute_calls >= 1
    assert any(event.kind == "shutdown_pending" for event in rig.events)
    await eventually(lambda: not rig.room.connected)
    assert not any(rig.room.listeners.values())
    assert rig.sources[0].close_calls == 0

    gate.release.set()
    await eventually(lambda: rig.sources[0].close_calls == 1)
    assert not gate.cancelled.is_set()
    assert rig.sources[0].queue == []
    assert rig.segments == []
    # 切断済み Room へ unpublish の応答待ちを残さない。
    assert rig.room.local_participant.unpublished == []


@pytest.mark.parametrize("event", ["track_muted", "track_unsubscribed"])
async def test_microphone_events_only_invalidate_pinned_participant_and_current_track(rig, event):
    await rig.transport.connect()
    await rig.audio.open(track_sid="TR-input", request_id="request", revision=1)
    response_id = rig.session.submit_text("合成入力")
    participant = SimpleNamespace(identity="fixture-user", sid="PA-fixture")
    publication = SimpleNamespace(sid="TR-input")

    def emit(owner, published):
        if event == "track_muted":
            rig.room.emit(event, owner, published)
        else:
            rig.room.emit(event, SimpleNamespace(), published, owner)

    emit(SimpleNamespace(identity="fixture-user", sid="PA-stale"), publication)
    emit(SimpleNamespace(identity="other-user", sid="PA-fixture"), publication)
    emit(participant, SimpleNamespace(sid="TR-unrelated"))
    assert rig.session.active == response_id
    assert rig.audio.backend.grant is not None

    emit(participant, publication)
    assert rig.session.active is None
    assert rig.audio.backend.grant is None
    await rig.transport.aclose()
    assert not any(rig.room.listeners.values())


async def test_disconnect_invalidates_before_provider_observes_cancellation(rig):
    gate = rig.gate(suppress_cancel=True)
    seen = []

    class Core:
        async def stream(self, text):
            gate.entered.set()
            try:
                await gate.release.wait()
            except asyncio.CancelledError:
                seen.append((rig.session.active, rig.session.playback.active,
                             rig.session.generation, rig.audio.backend.grant))
                gate.cancelled.set()
                await gate.release.wait()
            yield "遅着した応答。"

    rig.session.core = Core()
    await rig.transport.connect()
    await rig.audio.open(track_sid="TR-input", request_id="request", revision=1)
    rig.session.submit_text("合成入力")
    generation = rig.session.generation
    await gate.entered.wait()
    rig.room.emit("disconnected")

    assert rig.session.active is rig.session.playback.active is None
    assert rig.session.generation > generation
    assert rig.audio.backend.grant is None
    await gate.cancelled.wait()
    assert seen[0][0:2] == (None, None)
    assert seen[0][2] > generation
    assert seen[0][3] is None
    gate.release.set()
    await rig.transport.aclose()
    assert rig.sources == rig.segments == []
    assert any(event.kind == "input_reopen_required" for event in rig.events)


async def test_double_close_detaches_listeners_and_releases_owned_resources_once(rig):
    await rig.transport.connect()
    rig.session.submit_text("合成入力")
    await eventually(lambda: len(rig.segments) == 1)
    assert any(rig.room.listeners.values())

    await asyncio.gather(rig.transport.aclose(), rig.transport.aclose())
    await rig.transport.aclose()

    assert not any(rig.room.listeners.values())
    assert not rig.room.connected
    assert rig.room.disconnect_calls == 1
    assert rig.room.local_participant.unpublished == [rig.segments[0].track_sid]
    assert rig.sources[0].close_calls == rig.pipeline.close_calls == 1
    assert rig.sources[0].queue == []
    assert rig.tracks[0].mute_calls >= 1
    with pytest.raises(ValueError, match="session_closed"):
        rig.session.submit_text("終了後")
    with pytest.raises(RuntimeError, match="single_connection"):
        await rig.transport.connect()


async def test_connect_failure_sanitizes_error_and_closes_input(rig):
    rig.connect_error = RuntimeError("synthetic-secret provider-detail")
    with pytest.raises(RuntimeError, match="^livekit_connection_failed$"):
        await rig.transport.connect()

    assert not rig.room.connected
    assert not any(rig.room.listeners.values())
    assert rig.pipeline.close_calls == 1
    assert all("synthetic-secret" not in repr(event) for event in rig.events)
    with pytest.raises(RuntimeError, match="not_connected"):
        await rig.transport.open_input(track_sid="track", request_id="request", revision=1)


async def test_connect_timeout_closes_input_and_still_reclaims_late_native_connection(rig):
    gate = rig.connect_gate = rig.gate()
    connecting = asyncio.create_task(rig.transport.connect())
    await gate.entered.wait()
    with pytest.raises(RuntimeError, match="^livekit_connection_failed$"):
        await connecting

    assert not gate.cancelled.is_set()
    assert not any(rig.room.listeners.values())
    assert not rig.room.connected
    assert rig.pipeline.close_calls == 1
    gate.release.set()
    await rig.room.connect_completed.wait()
    await eventually(lambda: rig.room.disconnect_calls == 1)
    assert not rig.room.connected


async def test_cancel_connect_preserves_cancellation_and_reclaims_late_native_connection(rig):
    gate = rig.connect_gate = rig.gate(suppress_cancel=True)
    connecting = asyncio.create_task(rig.transport.connect())
    await gate.entered.wait()
    connecting.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await connecting

    assert not gate.cancelled.is_set()
    assert not any(rig.room.listeners.values())
    assert rig.pipeline.close_calls == 1
    assert any(event.kind == "shutdown_pending" for event in rig.events)
    gate.release.set()
    await rig.room.connect_completed.wait()
    await eventually(lambda: rig.room.disconnect_calls == 1)
    assert not rig.room.connected


@pytest.mark.parametrize("invalid_wav", [
    b"synthetic-private-content",
    wav_bytes(channels=2),
    wav_bytes(rate=8000),
    wav_bytes()[:-2],
])
async def test_invalid_wav_is_terminal_without_exposing_contents(rig, invalid_wav, caplog):
    rig.tts.wav = invalid_wav
    await rig.transport.connect()
    rig.session.submit_text("synthetic-private-content")
    await eventually(lambda: any(event.kind == "transport_failed" for event in rig.events))
    await rig.transport.aclose()

    failures = [event.detail for event in rig.events if event.kind == "transport_failed"]
    assert failures == ["output_delivery_failed"]
    assert rig.session.active is None
    assert rig.sources == rig.segments == []
    assert not rig.room.connected
    assert "synthetic-private-content" not in repr(rig.events) + caplog.text
    assert "playback_completed" not in [event.kind for event in rig.events]



@pytest.mark.parametrize("url", [
    "ws://localhost:7880", "ws://127.0.0.1:7880", "ws://[::1]:7880",
    "ws://LOCALHOST:7880", "wss://fixture.invalid", "wss://192.0.2.1",
    "wss://[2001:db8::1]",
])
def test_config_allows_explicit_loopback_ws_and_remote_wss(url):
    config = livekit_transport.LiveKitConfig(url, "synthetic-token", "user", "PA-user")
    assert config.url == url


@pytest.mark.parametrize("url", [
    "ws://fixture.invalid", "ws://192.0.2.1", "ws://192.168.1.10",
    "ws://0.0.0.0", "ws://[::]", "ws://[2001:db8::1]",
    "ws://localhost.fixture.invalid", "ws://127.0.0.1.fixture.invalid",
    "ws://user:secret@localhost", "wss://user:secret@fixture.invalid",
    "ws://localhost?token=secret", "wss://fixture.invalid#secret",
])
def test_config_rejects_cleartext_remote_urls_and_embedded_credentials(url):
    with pytest.raises(ValueError, match="^invalid_livekit_config$"):
        livekit_transport.LiveKitConfig(url, "synthetic-token", "user", "PA-user")
