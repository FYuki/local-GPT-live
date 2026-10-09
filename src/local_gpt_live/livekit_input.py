"""認証済みマイクを、欠落の見える有界 PCM 入力へ接続する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from livekit import rtc
from livekit.rtc._proto.stats_pb2 import RtcStats
from livekit.rtc.frame_processor import FrameProcessor
from livekit.rtc.room import EventTypes

from .input import AudioInput
from .session import Event
from .voice_input.pipeline import AudioInputFault
from .voice_input.session import InputGrant

READY_SECONDS = 4.0
VERIFY_SECONDS = 1.0
CLOSE_SECONDS = 1.0
_STAMP = "local-gpt-live.sample-position"


class _FrameClock(FrameProcessor[rtc.AudioFrame]):
    """SDK ring queue 投入前に位置を付け、読み取り側で欠落を隠さない。"""

    def __init__(self) -> None:
        self._owner = object()
        self._position = 0
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    @staticmethod
    def _validate(frame: rtc.AudioFrame) -> None:
        if (frame.sample_rate != 16_000 or frame.num_channels != 1
                or not 0 < frame.samples_per_channel <= 160
                or frame.data.nbytes != frame.samples_per_channel * 2):
            raise AudioInputFault("invalid_audio_frame")

    def _process(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        self._validate(frame)
        if not self.enabled:
            raise AudioInputFault("microphone_clock_unavailable")
        frame.userdata[_STAMP] = (self._owner, self._position, frame.samples_per_channel)
        self._position += frame.samples_per_channel
        return frame

    def read(self, frame: rtc.AudioFrame) -> tuple[bytes, int]:
        self._validate(frame)
        stamp = frame.userdata.get(_STAMP)
        if (not self.enabled or not isinstance(stamp, tuple) or len(stamp) != 3
                or stamp[0] is not self._owner or type(stamp[1]) is not int
                or stamp[2] != frame.samples_per_channel):
            # SDK は processor 例外時に元フレームを流すため、印なしを拒否する。
            raise AudioInputFault("microphone_position_unavailable")
        return bytes(frame.data), stamp[1]

    def _close(self) -> None:
        self._enabled = False


@dataclass(frozen=True)
class _Counters:
    identifier: str
    timestamp: int
    concealed: int
    silent: int


def _counters(stats: Sequence[RtcStats]) -> _Counters:
    inbound = [s.inbound_rtp for s in stats
               if s.HasField("inbound_rtp") and s.inbound_rtp.stream.kind == "audio"]
    if len(inbound) != 1:
        raise AudioInputFault("audio_integrity_unavailable")
    item = inbound[0]
    codecs = [s.codec.codec for s in stats
              if s.HasField("codec") and s.codec.rtc.id == item.stream.codec_id]
    if (len(codecs) != 1 or codecs[0].mime_type.lower() != "audio/opus"
            or codecs[0].clock_rate != 48_000 or not item.rtc.id
            or not item.rtc.HasField("timestamp")
            or not item.inbound.HasField("concealed_samples")
            or not item.inbound.HasField("silent_concealed_samples")
            or item.inbound.silent_concealed_samples > item.inbound.concealed_samples):
        raise AudioInputFault("audio_integrity_unavailable")
    return _Counters(item.rtc.id, item.rtc.timestamp, item.inbound.concealed_samples,
                     item.inbound.silent_concealed_samples)


async def _observe(track: rtc.RemoteTrack,
                   remember: Callable[[asyncio.Task[list[RtcStats]]], None],
                   previous: _Counters | None = None, *,
                   ready_deadline: float | None = None) -> _Counters:
    deadline = asyncio.get_running_loop().time() + (
        READY_SECONDS if previous is None else VERIFY_SECONDS
    )
    if previous is None and ready_deadline is not None:
        deadline = min(deadline, ready_deadline)
    try:
        async with asyncio.timeout_at(deadline):
            while True:
                operation = asyncio.create_task(track.get_stats())
                remember(operation)
                try:
                    done, _ = await asyncio.wait(
                        [operation], timeout=max(0, deadline - asyncio.get_running_loop().time())
                    )
                    if not done or asyncio.get_running_loop().time() >= deadline:
                        operation.cancel()
                        raise TimeoutError
                    current = _counters(operation.result())
                except asyncio.CancelledError:
                    operation.cancel()
                    raise
                except TimeoutError:
                    raise
                except Exception:
                    if previous is not None:
                        raise AudioInputFault("audio_integrity_unavailable") from None
                else:
                    if previous is None:
                        return current
                    if (current.identifier != previous.identifier
                            or current.concealed < previous.concealed
                            or current.silent < previous.silent):
                        raise AudioInputFault("audio_integrity_unavailable")
                    if current.timestamp > previous.timestamp:
                        missing = (current.concealed - previous.concealed
                                   - current.silent + previous.silent)
                        if missing < 0:
                            raise AudioInputFault("audio_integrity_unavailable")
                        if missing >= 3_840:  # Opus 48kHz の非無音補完 80ms。
                            raise AudioInputFault("audio_gap")
                        return current
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise AudioInputFault("audio_integrity_unavailable") from None
    except asyncio.CancelledError:
        # 共通期限の取消でも、統計待機の既存失敗分類を保つ。
        if asyncio.get_running_loop().time() >= deadline:
            raise AudioInputFault("audio_integrity_unavailable") from None
        raise


class LiveKitInput:
    def __init__(self, audio: AudioInput, room: rtc.Room, identity: str,
                 participant_sid: str) -> None:
        self.audio, self.room = audio, room
        self.identity, self.participant_sid = identity, participant_sid
        self._reader: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._opens: set[asyncio.Task[object]] = set()
        self._observations: set[asyncio.Task[list[RtcStats]]] = set()
        self._streams: dict[rtc.AudioStream, _FrameClock] = {}
        self._opening = asyncio.Lock()
        self._epoch = 0
        self._closed = False
        self._prepared: tuple[InputGrant, rtc.RemoteTrack, _Counters] | None = None

    def _ready_track(self, track_sid: str) -> rtc.RemoteTrack | None:
        participant = self.room.remote_participants.get(self.identity)
        if (participant is None or participant.identity != self.identity
                or participant.sid != self.participant_sid):
            raise AudioInputFault("microphone_participant_unavailable")
        publication = participant.track_publications.get(track_sid)
        if publication is None:
            return None
        if (publication.sid != track_sid
                or publication.source != rtc.TrackSource.SOURCE_MICROPHONE
                or publication.kind != rtc.TrackKind.KIND_AUDIO
                or publication.muted):
            raise AudioInputFault("microphone_track_unavailable")
        track = publication.track
        if track is not None and (track.sid != track_sid
                                 or track.kind != rtc.TrackKind.KIND_AUDIO or track.muted):
            raise AudioInputFault("microphone_track_unavailable")
        return track if publication.subscribed else None

    def _track(self, track_sid: str) -> rtc.RemoteTrack:
        track = self._ready_track(track_sid)
        if track is None:
            raise AudioInputFault("microphone_track_unavailable")
        return track

    def _watch_preparation(self, track_sid: str, epoch: int,
                           changed: asyncio.Event) -> list[tuple[EventTypes, Callable[..., None]]]:
        handlers: list[tuple[EventTypes, Callable[..., None]]] = []

        def invalidate(*_: object) -> None:
            if handlers and epoch == self._epoch:
                self.stop(reason="microphone_track_unavailable")

        def target(participant: rtc.Participant, sid: str) -> bool:
            return (participant.identity == self.identity
                    and participant.sid == self.participant_sid and sid == track_sid)

        def published(publication: rtc.RemoteTrackPublication,
                      participant: rtc.RemoteParticipant) -> None:
            if handlers and epoch == self._epoch and target(participant, publication.sid):
                # 通知は再検証の契機に限り、認可は現在のRoom状態から行う。
                try:
                    self._ready_track(track_sid)
                except AudioInputFault:
                    invalidate()
                changed.set()

        def subscribed(_track: rtc.RemoteTrack, publication: rtc.RemoteTrackPublication,
                       participant: rtc.RemoteParticipant) -> None:
            published(publication, participant)

        def unpublished(publication: rtc.RemoteTrackPublication,
                        participant: rtc.RemoteParticipant) -> None:
            if target(participant, publication.sid):
                invalidate()

        def unsubscribed(_track: rtc.RemoteTrack | None, publication: rtc.RemoteTrackPublication,
                         participant: rtc.RemoteParticipant) -> None:
            unpublished(publication, participant)

        def muted(participant: rtc.Participant, publication: rtc.TrackPublication) -> None:
            if target(participant, publication.sid):
                invalidate()

        def failed(participant: rtc.RemoteParticipant, sid: str, _error: str) -> None:
            if target(participant, sid):
                invalidate()

        def left(participant: rtc.RemoteParticipant) -> None:
            if participant.identity == self.identity and participant.sid == self.participant_sid:
                invalidate()

        def joined(participant: rtc.RemoteParticipant) -> None:
            if participant.identity == self.identity and participant.sid != self.participant_sid:
                invalidate()

        registrations: list[tuple[EventTypes, Callable[..., None]]] = [
            ("track_published", published), ("track_subscribed", subscribed),
            ("track_unpublished", unpublished), ("track_unsubscribed", unsubscribed),
            ("track_muted", muted), ("track_subscription_failed", failed),
            ("participant_disconnected", left), ("participant_connected", joined),
            ("disconnected", invalidate), ("reconnecting", invalidate),
        ]
        try:
            for event, handler in registrations:
                self.room.on(event, handler)
                handlers.append((event, handler))
        except BaseException:
            for event, handler in handlers:
                self.room.off(event, handler)
            handlers.clear()
            raise
        return handlers

    def _remember(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    def _observe_task(self, task: asyncio.Task[list[RtcStats]]) -> None:
        self._observations.add(task)

        def finished(done: asyncio.Task[list[RtcStats]]) -> None:
            self._observations.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def open(self, *, track_sid: str, request_id: str, revision: int) -> InputGrant:
        grant = await self.prepare(track_sid=track_sid, request_id=request_id, revision=revision)
        if not self.start(grant):
            raise AudioInputFault("microphone_stream_unavailable")
        return grant

    async def prepare(self, *, track_sid: str, request_id: str, revision: int,
                      deadline: float | None = None) -> InputGrant:
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + READY_SECONDS
        task = asyncio.current_task()
        assert task is not None
        self._opens.add(task)
        try:
            async with asyncio.timeout_at(deadline):
                return await self._prepare(track_sid=track_sid, request_id=request_id,
                                           revision=revision, deadline=deadline)
        except TimeoutError:
            raise AudioInputFault("microphone_track_unavailable") from None
        finally:
            self._opens.discard(task)

    def _cleanup_pending(self) -> bool:
        return (
            any(not task.done() for task in self._observations)
            or any(not task.done() and task is not self._reader for task in self._tasks)
            or bool(self._streams and self._reader is not None
                    and (self._reader.done() or self._reader.cancelling()))
        )

    async def wait_for_cleanup(self) -> None:
        while self._cleanup_pending():
            owned = tuple(task for task in self._tasks | self._observations
                          if not task.done()
                          and (task is not self._reader or task.cancelling()))
            if owned:
                await asyncio.wait(owned)
            else:
                # 終了readerのretire callbackが新たなrelease taskを所有する。
                await asyncio.sleep(0)

    async def _prepare(self, *, track_sid: str, request_id: str, revision: int,
                       deadline: float) -> InputGrant:
        async with self._opening:
            if self._closed:
                raise AudioInputFault("audio_input_closed")
            if (not isinstance(track_sid, str) or not track_sid
                    or not isinstance(request_id, str) or not request_id
                    or type(revision) is not int or revision <= 0):
                raise AudioInputFault("invalid_input_request")
            self._ready_track(track_sid)
            old = self.audio.backend.grant
            if (old is not None
                    and (self._prepared is not None
                         or self._reader is not None and not self._reader.done())
                    and (track_sid, request_id, revision)
                    == (old.track_sid, old.request_id, old.input_revision)):
                self._track(track_sid)
                return old
            if revision <= self.audio.backend.revision:
                raise AudioInputFault("stale_input_request")
            if self._cleanup_pending():
                raise AudioInputFault("microphone_cleanup_pending")
            epoch = self._epoch
            changed = asyncio.Event()
            handlers = self._watch_preparation(track_sid, epoch, changed)
            try:
                while True:
                    changed.clear()
                    track = self._ready_track(track_sid)
                    if track is not None:
                        break
                    await changed.wait()
                counters = await _observe(track, self._observe_task, ready_deadline=deadline)
                if self._closed or epoch != self._epoch or self._track(track_sid) is not track:
                    raise AudioInputFault("stale_input_request")
                if self._cleanup_pending():
                    raise AudioInputFault("microphone_cleanup_pending")
                grant = await self.audio.open(track_sid=track_sid, request_id=request_id,
                                              revision=revision)
                try:
                    if (self._closed or epoch != self._epoch
                            or asyncio.get_running_loop().time() >= deadline
                            or self._track(track_sid) is not track):
                        raise AudioInputFault("stale_input_request")
                except Exception:
                    if self.audio.backend.grant is grant:
                        self.stop(reason="input_open_failed")
                    raise
                self._prepared = (grant, track, counters)
                return grant
            finally:
                for event, handler in handlers:
                    self.room.off(event, handler)
                handlers.clear()

    def start(self, grant: InputGrant) -> bool:
        if self._closed or self.audio.backend.grant is not grant:
            return False
        if self._prepared is None:
            return self._reader is not None and not self._reader.done()
        saved, track, counters = self._prepared
        if saved is not grant:
            return False
        clock = _FrameClock()
        try:
            if self._track(grant.track_sid) is not track:
                raise AudioInputFault("stale_input_request")
            stream = rtc.AudioStream(track, sample_rate=16_000, num_channels=1,
                                     capacity=160, frame_size_ms=10, noise_cancellation=clock)
        except Exception:
            self.stop(reason="input_open_failed")
            return False
        self._prepared = None
        if self._reader is not None:
            self._reader.cancel()
        self._reader = asyncio.create_task(
            self._read(stream, clock, track, counters, grant, self._epoch),
        )
        self._streams[stream] = clock
        self._reader.add_done_callback(lambda _: self._retire(stream))
        self._remember(self._reader)
        return True

    def stop(self, *, revision: int | None = None, reason: str = "input_stopped") -> bool:
        changed = self.audio.suppress(
            revision=self.audio.backend.revision + 1 if revision is None else revision,
            reason=reason,
        )
        if changed or revision is None:
            self._prepared = None
            self._epoch += 1
            if self._reader is not None and self._reader is not asyncio.current_task():
                self._reader.cancel()
            for task in tuple(self._opens):
                if task is not asyncio.current_task():
                    task.cancel()
        if changed:
            self.audio.session.emit(Event("input_invalidated"))
        return changed

    def _retire(self, stream: rtc.AudioStream) -> None:
        clock = self._streams.pop(stream, None)
        if clock is not None:
            clock._close()
            self._remember(asyncio.create_task(self._release(stream)))

    async def _release(self, stream: rtc.AudioStream) -> None:
        operation = asyncio.create_task(stream.aclose())
        self._remember(operation)
        try:
            done, _ = await asyncio.wait([operation], timeout=CLOSE_SECONDS)
            if not done:
                operation.cancel()
                raise TimeoutError
            operation.result()
        except Exception:
            self.audio.session.emit(Event("shutdown_pending", detail="microphone_stream_not_drained"))

    async def _read(self, stream: rtc.AudioStream, clock: _FrameClock, track: rtc.RemoteTrack,
                    counters: _Counters, grant: InputGrant, epoch: int) -> None:
        position = 0
        batch: list[tuple[bytes, int]] = []
        try:
            async for event in stream:
                if self.audio.backend.grant is not grant:
                    return
                pcm, start = clock.read(event.frame)
                if start != position:
                    raise AudioInputFault("audio_gap")
                position += len(pcm) // 2
                batch.append((pcm, start))
                if len(batch) < 10:
                    continue
                # 取得前に届いた PCM のみ、前進した統計の確認後に後段へ流す。
                counters = await _observe(track, self._observe_task, counters)
                if self._track(grant.track_sid) is not track:
                    raise AudioInputFault("microphone_track_unavailable")
                for pcm, start in batch:
                    await self.audio.backend.receive(pcm, start_sample=start, grant=grant)
                batch.clear()
            if self.audio.backend.grant is grant:
                self.stop(reason="microphone_stream_ended")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Backendがこの操作のgrantを内部失効しても、現行readerの失敗は通知する。
            # 停止・交代後の遅着は現在の操作へ作用させない。
            if (not self._closed and self._epoch == epoch
                    and self._reader is asyncio.current_task()
                    and (self.audio.backend.grant is grant or self.audio.backend.grant is None)):
                reason = error.code if isinstance(error, AudioInputFault) else "microphone_read_failed"
                self.stop(reason=reason)
                self.audio.session.emit(Event("input_rejected", detail=reason))

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self.stop(reason="input_closed")
        for stream in tuple(self._streams):
            self._retire(stream)
        owned = tuple(self._tasks | self._opens | self._observations)
        if owned:
            _, pending = await asyncio.wait(owned, timeout=CLOSE_SECONDS * 2)
            for task in pending:
                task.cancel()
            if pending:
                self.audio.session.emit(Event("shutdown_pending", detail="microphone_reader_not_drained"))
