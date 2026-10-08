"""実接続なしで、SDK 入力境界・取消・有界解放を合成検証する。"""

# SDK extra が未導入の環境では、この transport テストだけを skip する。
# ruff: noqa: E402

import asyncio
from types import SimpleNamespace

import pytest

rtc = pytest.importorskip("livekit.rtc")

from livekit.rtc._proto.stats_pb2 import RtcStats
from livekit.rtc._utils import RingQueue

from local_gpt_live import livekit_input as module
from local_gpt_live.demo import FixtureCore, FixtureStt, FixtureTts
from local_gpt_live.input import AudioInput
from local_gpt_live.livekit_input import LiveKitInput
from local_gpt_live.playback import Playback
from local_gpt_live.session import VoiceSession
from local_gpt_live.voice_input.pipeline import AudioInputFault


def statistics(timestamp=1, concealed=0, silent=0, identifier="rtp"):
    inbound = RtcStats()
    inbound.inbound_rtp.rtc.id = identifier
    inbound.inbound_rtp.rtc.timestamp = timestamp
    inbound.inbound_rtp.stream.kind = "audio"
    inbound.inbound_rtp.stream.codec_id = "opus"
    inbound.inbound_rtp.inbound.concealed_samples = concealed
    inbound.inbound_rtp.inbound.silent_concealed_samples = silent
    codec = RtcStats()
    codec.codec.rtc.id = "opus"
    codec.codec.codec.mime_type = "audio/opus"
    codec.codec.codec.clock_rate = 48_000
    return [inbound, codec]


class Pipeline:
    def __init__(self):
        self.received = []
        self.closed = False

    def reset(self, next_sample=None, *, quarantine=False):
        pass

    def feed(self, pcm, *, start_sample):
        self.received.append((pcm, start_sample))
        return ()

    def close(self):
        self.closed = True


class Track:
    sid = "microphone"
    kind = rtc.TrackKind.KIND_AUDIO
    muted = False

    def __init__(self):
        self.calls = 0
        self.concealed = 0
        self.silent = 0
        self.identifier = "rtp"

    async def get_stats(self):
        self.calls += 1
        return statistics(self.calls, self.concealed, self.silent, self.identifier)


class Stream:
    instances = []

    def __init__(self, track, **options):
        self.options = options
        self.clock = options["noise_cancellation"]
        self.queue = RingQueue(options["capacity"])
        self.closed = 0
        self.instances.append(self)

    def push(self, *, stamped=True):
        frame = rtc.AudioFrame(bytes(320), 16_000, 1, 160)
        if stamped:
            frame = self.clock._process(frame)
        self.queue.put(rtc.AudioFrameEvent(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.queue.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def aclose(self):
        self.closed += 1
        self.queue.put(None)


@pytest.fixture
async def harness(monkeypatch):
    Stream.instances = []
    monkeypatch.setattr(module.rtc, "AudioStream", Stream)
    monkeypatch.setattr(module, "READY_SECONDS", 0.08)
    monkeypatch.setattr(module, "VERIFY_SECONDS", 0.08)
    monkeypatch.setattr(module, "CLOSE_SECONDS", 0.05)
    events = []
    session = VoiceSession(FixtureStt(), FixtureCore(), FixtureTts(), Playback(), emit=events.append)
    pipeline = Pipeline()
    audio = AudioInput(session, pipeline)
    track = Track()
    publication = SimpleNamespace(sid=track.sid, track=track, subscribed=True, muted=False,
                                  kind=rtc.TrackKind.KIND_AUDIO,
                                  source=rtc.TrackSource.SOURCE_MICROPHONE)
    participant = SimpleNamespace(identity="browser", sid="participant",
                                  track_publications={track.sid: publication})
    room = SimpleNamespace(remote_participants={participant.identity: participant})
    bridge = LiveKitInput(audio, room, participant.identity, participant.sid)
    yield SimpleNamespace(bridge=bridge, audio=audio, pipeline=pipeline, events=events,
                          track=track, publication=publication, participant=participant, room=room)
    await bridge.aclose()
    await audio.close()


async def opened(h):
    grant = await h.bridge.open(track_sid=h.track.sid, request_id="request", revision=1)
    return grant, Stream.instances[-1]


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("outcome", ["error", "end"])
async def test_reader_failure_only_invalidates_its_current_operation(harness, monkeypatch, stopped,
                                                                   outcome):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()

    class FailingStream(Stream):
        async def __anext__(self):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            if outcome == "end":
                raise StopAsyncIteration
            raise RuntimeError("synthetic-native-secret-reader")

    monkeypatch.setattr(module.rtc, "AudioStream", FailingStream)
    grant, stream = await opened(h)
    await entered.wait()
    if stopped:
        h.bridge.stop(reason="muted")
    before_revision = h.audio.backend.revision
    before_events = len(h.events)
    release.set()
    await until(lambda: stream.closed == 1)
    if stopped:
        assert h.audio.backend.revision == before_revision
        assert len(h.events) == before_events
    else:
        assert h.audio.backend.grant is None
        assert h.audio.backend.revision > before_revision
        expected = ["input_invalidated"]
        if outcome == "error":
            expected.append("input_rejected")
        assert [e.kind for e in h.events[before_events:]] == expected
        if outcome == "error":
            assert h.events[-1].detail == "microphone_read_failed"
    assert h.audio.backend.grant is not grant


async def test_direct_input_internal_backend_loss_is_notified(harness, monkeypatch):
    h = harness

    def failing_feed(pcm, *, start_sample):
        raise RuntimeError("synthetic-native-secret-pcm")

    def failing_reset(next_sample=None, *, quarantine=False):
        if quarantine:
            raise RuntimeError("synthetic-native-secret-reset")

    monkeypatch.setattr(h.pipeline, "feed", failing_feed)
    monkeypatch.setattr(h.pipeline, "reset", failing_reset)
    _, stream = await opened(h)
    for _ in range(10):
        stream.push()
    await until(lambda: stream.closed == 1)
    assert h.audio.backend.grant is None
    assert [e.kind for e in h.events][-2:] == ["input_invalidated", "input_rejected"]
    assert h.events[-1].detail == "vad_unavailable"


async def test_verified_batch_reaches_real_backend_with_sdk_sample_positions(harness):
    h = harness
    grant, stream = await opened(h)
    assert stream.options["sample_rate"] == 16_000
    assert stream.options["num_channels"] == 1
    assert stream.options["frame_size_ms"] == 10
    assert stream.options["capacity"] == 160
    for _ in range(9):
        stream.push()
    await asyncio.sleep(0.01)
    assert h.pipeline.received == []
    stream.push()
    await until(lambda: len(h.pipeline.received) == 10)
    assert [start for _, start in h.pipeline.received] == list(range(0, 1600, 160))
    assert h.track.calls == 2
    assert await h.bridge.open(track_sid=h.track.sid, request_id="request", revision=1) is grant
    assert len(Stream.instances) == 1
    assert h.bridge.stop(revision=1) is False
    assert h.audio.backend.grant is grant


@pytest.mark.parametrize("attribute,value", [
    ("participant.sid", "rejoined"), ("participant.identity", "other"),
    ("publication.source", rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO),
    ("publication.muted", True), ("publication.subscribed", False),
    ("publication.sid", "other"), ("track.sid", "other"), ("track.muted", True),
    ("track.kind", rtc.TrackKind.KIND_VIDEO),
])
async def test_untrusted_or_unavailable_microphone_is_never_opened(harness, attribute, value):
    target, name = attribute.split(".")
    setattr(getattr(harness, target), name, value)
    with pytest.raises(AudioInputFault):
        await harness.bridge.open(track_sid="microphone", request_id="request", revision=1)
    assert harness.audio.backend.grant is None
    assert Stream.instances == []


async def test_missing_statistics_never_grants_input(harness):
    async def unavailable():
        return []
    harness.track.get_stats = unavailable
    with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
        await opened(harness)
    assert harness.audio.backend.grant is None
    assert Stream.instances == []


@pytest.mark.parametrize("concealed,silent,rejected", [(3840, 0, True), (4000, 4000, False)])
async def test_concealment_is_checked_before_accepting_the_batch(harness, concealed, silent, rejected):
    h = harness
    _, stream = await opened(h)
    h.track.concealed, h.track.silent = concealed, silent
    for _ in range(10):
        stream.push()
    await until(lambda: h.audio.backend.grant is None if rejected else h.pipeline.received)
    if rejected:
        assert h.pipeline.received == []
        assert any(e.detail == "audio_gap" for e in h.events)
    else:
        await until(lambda: len(h.pipeline.received) == 10)


@pytest.mark.parametrize("overflow", [False, True])
async def test_missing_stamp_or_ring_queue_drop_fails_closed(harness, overflow):
    h = harness
    _, stream = await opened(h)
    if overflow:
        # 読み取り task を動かす前に SDK 同等の ring queue を溢れさせる。
        for _ in range(161):
            stream.push()
    else:
        stream.push(stamped=False)
    await until(lambda: h.audio.backend.grant is None)
    assert h.pipeline.received == []
    assert any(e.detail == ("audio_gap" if overflow else "microphone_position_unavailable")
               for e in h.events)


async def test_close_before_reader_first_runs_still_closes_stream_once(harness):
    h = harness
    grant, stream = await opened(h)
    assert h.bridge.stop(reason="disconnected")
    await h.bridge.aclose()
    await h.bridge.aclose()
    assert stream.closed == 1
    assert h.audio.backend.grant is None
    assert not h.pipeline.closed  # AudioInput の所有者は親 adapter。
    await h.audio.backend.receive(bytes(320), start_sample=0, grant=grant)
    assert h.pipeline.received == []


async def test_stop_cancels_pending_stats_even_when_sdk_suppresses_cancel(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return statistics()

    h.track.get_stats = blocked
    pending = asyncio.create_task(opened(h))
    await entered.wait()
    h.bridge.stop(reason="disconnected")
    with pytest.raises(asyncio.CancelledError):
        await pending
    try:
        await asyncio.wait_for(h.bridge.aclose(), timeout=0.3)
        assert h.audio.backend.grant is None
        assert Stream.instances == []
        assert any(e.kind == "shutdown_pending" for e in h.events)
    finally:
        release.set()
        await until(lambda: not h.bridge._observations)


async def test_constructor_failure_is_sanitized_and_revokes_grant(harness, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("sensitive SDK failure")
    monkeypatch.setattr(module.rtc, "AudioStream", failed)
    with pytest.raises(AudioInputFault, match="^microphone_stream_unavailable$"):
        await opened(harness)
    assert harness.audio.backend.grant is None


async def test_participant_changed_during_backend_reset_is_rejected(harness, monkeypatch):
    h = harness
    original = h.audio.open

    async def replaced(**kwargs):
        grant = await original(**kwargs)
        h.participant.sid = "rejoined"
        return grant

    monkeypatch.setattr(h.audio, "open", replaced)
    with pytest.raises(AudioInputFault, match="microphone_participant_unavailable"):
        await opened(h)
    assert h.audio.backend.grant is None
    assert Stream.instances == []


async def test_late_stats_after_readiness_deadline_cannot_grant(harness):
    h = harness
    release = asyncio.Event()

    async def late():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return statistics()

    h.track.get_stats = late
    try:
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await asyncio.wait_for(opened(h), timeout=0.3)
        assert h.audio.backend.grant is None
    finally:
        release.set()
        await until(lambda: not h.bridge._observations)


async def test_hung_stream_close_is_bounded_and_reported(harness, monkeypatch):
    h = harness
    _, stream = await opened(h)
    release = asyncio.Event()

    async def late_close():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        stream.closed += 1

    monkeypatch.setattr(stream, "aclose", late_close)
    try:
        await asyncio.wait_for(h.bridge.aclose(), timeout=0.3)
        assert any(e.detail == "microphone_stream_not_drained" for e in h.events)
    finally:
        release.set()
        await until(lambda: not h.bridge._tasks)


@pytest.mark.parametrize("fault", ["stale", "changed", "regressed", "missing"])
async def test_unverifiable_batch_is_never_forwarded(harness, fault):
    h = harness
    h.track.concealed = 100
    _, stream = await opened(h)

    async def broken_stats():
        if fault == "missing":
            return []
        return statistics(timestamp=1 if fault == "stale" else 2,
                          concealed=0 if fault == "regressed" else 100,
                          identifier="replaced" if fault == "changed" else "rtp")

    h.track.get_stats = broken_stats
    for _ in range(10):
        stream.push()
    await until(lambda: h.audio.backend.grant is None)
    assert h.pipeline.received == []
    assert any(e.detail == "audio_integrity_unavailable" for e in h.events)


async def test_idempotent_request_still_rejects_boolean_revision(harness):
    grant, _ = await opened(harness)
    with pytest.raises(AudioInputFault, match="invalid_input_request"):
        await harness.bridge.open(track_sid="microphone", request_id="request", revision=True)
    assert harness.audio.backend.grant is grant


async def test_stop_after_audio_owner_closed_still_releases_reader(harness):
    h = harness
    _, stream = await opened(h)
    await h.audio.close()
    await h.bridge.aclose()
    assert stream.closed == 1
    assert not h.bridge._tasks


async def test_new_track_replaces_old_grant_and_disposes_only_old_stream(harness):
    h = harness
    old_grant, old_stream = await opened(h)
    new_track = Track()
    new_track.sid = "new-microphone"
    new_publication = SimpleNamespace(**vars(h.publication))
    new_publication.sid, new_publication.track = new_track.sid, new_track
    h.participant.track_publications[new_track.sid] = new_publication
    new_grant = await h.bridge.open(track_sid=new_track.sid, request_id="new", revision=2)
    new_stream = Stream.instances[-1]
    await until(lambda: old_stream.closed == 1)
    assert not new_stream.closed
    await h.audio.backend.receive(bytes(320), start_sample=0, grant=old_grant)
    assert h.pipeline.received == []
    for _ in range(10):
        new_stream.push()
    await until(lambda: len(h.pipeline.received) == 10)
    assert h.audio.backend.grant is new_grant


async def test_repeated_open_cannot_accumulate_cancel_resistant_stats_tasks(harness):
    h = harness
    release = asyncio.Event()
    original = h.track.get_stats
    calls = 0

    async def late():
        nonlocal calls
        calls += 1
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return statistics()

    h.track.get_stats = late
    try:
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await opened(h)
        for _ in range(4):
            with pytest.raises(AudioInputFault, match="^microphone_cleanup_pending$"):
                await opened(h)
        assert calls == 1
        assert len(h.bridge._observations) == 1
        assert Stream.instances == []
    finally:
        release.set()
        await until(lambda: not h.bridge._observations)
    h.track.get_stats = original
    grant, _ = await opened(h)
    assert h.audio.backend.grant is grant


async def test_repeated_open_waits_for_previous_stream_cleanup(harness, monkeypatch):
    h = harness
    _, old_stream = await opened(h)
    release, entered = asyncio.Event(), asyncio.Event()

    async def late_close():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        old_stream.closed += 1

    monkeypatch.setattr(old_stream, "aclose", late_close)
    h.bridge.stop(reason="muted")
    new_track = Track()
    new_track.sid = "replacement"
    publication = SimpleNamespace(**vars(h.publication))
    publication.sid, publication.track = new_track.sid, new_track
    h.participant.track_publications[new_track.sid] = publication

    async def reopen():
        return await h.bridge.open(track_sid=new_track.sid, request_id="replacement", revision=3)

    try:
        # reader の終了 callback が実行される前でも再openを許可しない。
        with pytest.raises(AudioInputFault, match="^microphone_cleanup_pending$"):
            await reopen()
        await entered.wait()
        await until(lambda: any(e.detail == "microphone_stream_not_drained" for e in h.events))
        for _ in range(4):
            with pytest.raises(AudioInputFault, match="^microphone_cleanup_pending$"):
                await reopen()
        assert len(Stream.instances) == 1
        assert sum(not task.done() for task in h.bridge._tasks) == 1
    finally:
        release.set()
        await until(lambda: not h.bridge._tasks)
    grant = await reopen()
    assert h.audio.backend.grant is grant
    assert old_stream.closed == 1
    assert len(Stream.instances) == 2


async def test_prepared_input_waits_for_ack_before_reading_pcm(harness):
    h = harness
    grant = await h.bridge.prepare(track_sid=h.track.sid, request_id="request", revision=1)
    assert h.track.calls > 0
    assert h.pipeline.received == []
    assert Stream.instances == []
    assert h.bridge.start(grant)
    stream = Stream.instances[-1]
    for _ in range(10):
        stream.push()
    await until(lambda: len(h.pipeline.received) == 10)
    assert [start for _, start in h.pipeline.received] == list(range(0, 1600, 160))


async def test_stopped_preparation_cannot_start_or_replace_new_input(harness):
    h = harness
    old = await h.bridge.prepare(track_sid=h.track.sid, request_id="old", revision=1)
    h.bridge.stop(reason="disconnected")
    assert not h.bridge.start(old)
    assert h.audio.backend.grant is None
    assert Stream.instances == []
    new_track = Track()
    new_track.sid = "replacement"
    publication = SimpleNamespace(**vars(h.publication))
    publication.sid, publication.track = new_track.sid, new_track
    h.participant.track_publications[new_track.sid] = publication
    new = await h.bridge.prepare(track_sid=new_track.sid, request_id="new", revision=3)
    assert not h.bridge.start(old)
    assert h.bridge.start(new)
    for _ in range(10):
        Stream.instances[-1].push()
    await until(lambda: len(h.pipeline.received) == 10)
    assert h.audio.backend.grant == new


async def test_start_rechecks_participant_after_preparation(harness):
    h = harness
    grant = await h.bridge.prepare(track_sid=h.track.sid, request_id="request", revision=1)
    h.participant.sid = "rejoined"
    assert not h.bridge.start(grant)
    assert h.audio.backend.grant is None
    assert Stream.instances == []
