"""既存の音声境界を公式LiveKit RTC SDKへ接続する。実再生ACKは生成しない。"""

from __future__ import annotations

import asyncio
import io
import math
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from livekit import rtc
from livekit.rtc.room import EventTypes

from .input import AudioInput
from .livekit_input import LiveKitInput
from .playback import AudioPacket, Playback
from .session import Event
from .sent_audio import SentAudioProgress, SentAudioScope, SentAudioSnapshot
from .voice_input.session import InputGrant


_OUTPUT_QUEUE_MS = 100


@dataclass(frozen=True)
class LiveKitConfig:
    url: str
    token: str = field(repr=False)
    participant_identity: str
    participant_sid: str
    connect_timeout: float = 10
    close_timeout: float = 2
    output_ready_timeout: float = 3
    estimated_downlink_delay: float = 0.3

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme not in {"ws", "wss"} or not parsed.hostname
                or (parsed.scheme == "ws" and not loopback)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not self.token.strip() or not self.participant_identity
                or not self.participant_sid):
            raise ValueError("invalid_livekit_config")
        if any(not math.isfinite(t) or t <= 0
               for t in (self.connect_timeout, self.close_timeout, self.output_ready_timeout)):
            raise ValueError("invalid_livekit_timeout")
        if (not math.isfinite(self.estimated_downlink_delay)
                or self.estimated_downlink_delay < 0
                or not math.isfinite(self.estimated_downlink_delay * 1e9)):
            raise ValueError("invalid_livekit_estimated_delay")


@dataclass(frozen=True)
class TrackPublished:
    response_id: str
    track_sid: str


@dataclass(frozen=True)
class SegmentSent:
    """SDKに渡した論理sample範囲。実再生済みを表さない。"""

    response_id: str
    sequence: int
    track_sid: str
    sample_rate: int
    sample_start: int
    sample_end: int


class LiveKitPlayback(Playback):
    def __init__(self, max_bytes: int = 4_000_000) -> None:
        super().__init__(max_bytes)
        self._wake = asyncio.Event()
        self._invalidate: Callable[[], None] = lambda: None
        self._attached = False

    def enqueue(self, packet: AudioPacket) -> bool:
        accepted = super().enqueue(packet)
        if accepted:
            self._wake.set()
        return accepted

    def stop(self) -> None:
        super().stop()
        self._invalidate()
        self._wake.set()


@dataclass
class _Output:
    response_id: str
    source: rtc.AudioSource
    track: rtc.LocalAudioTrack
    sample_rate: int
    generation: int
    progress: SentAudioProgress | None = None
    scope: SentAudioScope | None = None
    sid: str | None = None
    samples: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def stop(self) -> None:
        try:
            self.source.clear_queue()
        finally:
            self.track.mute()


class LiveKitTransport:
    """Room、入出力task、SDK資源を所有する単回接続アダプター。"""

    def __init__(
        self, audio: AudioInput, config: LiveKitConfig, *,
        on_segment: Callable[[SegmentSent], None] = lambda segment: None,
        on_track: Callable[[TrackPublished], None] = lambda track: None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        playback = audio.session.playback
        if not isinstance(playback, LiveKitPlayback) or playback._attached:
            raise ValueError("livekit_playback_required")
        if playback.active is not None:
            raise ValueError("livekit_attach_before_response")
        self.audio, self.config, self.playback = audio, config, playback
        self.room = rtc.Room()
        self.input = LiveKitInput(audio, self.room, config.participant_identity,
                                  config.participant_sid)
        self._on_segment = on_segment
        self._on_track = on_track
        self._clock_ns = clock_ns
        self._progress: tuple[str, SentAudioProgress] | None = None
        self._previous_progress: tuple[str, SentAudioProgress] | None = None
        self._output: _Output | None = None
        self._pump: asyncio.Task[None] | None = None
        self._sending: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._connecting: asyncio.Task[None] | None = None
        self._connected = self._started = self._closed = False
        self._disconnecting = False
        self._disconnect_lock = asyncio.Lock()
        self._handlers: list[tuple[EventTypes, Callable[..., None]]] = []
        playback._invalidate = self._invalidate
        playback._attached = True

    def _report(self, reason: str) -> None:
        self.audio.session.emit(Event("transport_failed", detail=reason))

    def _invalidate(self) -> None:
        if self._output is not None:
            self._output.ready.set()
            try:
                try:
                    if self._output.progress is not None:
                        self._output.progress.freeze(self._clock_ns())
                finally:
                    self._output.stop()
            except Exception:
                self._report("output_stop_failed")
        # SDKのFFI完了を見失わないよう送信task自体は取消しない。
        # 遅着後にresponseを再照合してqueueを消し、公開SIDを回収する。

    def sent_audio_progress(self, response_id: str, *,
                            at_ns: int | None = None) -> SentAudioSnapshot | None:
        """保持中の送出範囲と経過時間による推定を取得する。ACKは変更しない。"""
        for entry in (self._progress, self._previous_progress):
            if entry is not None and entry[0] == response_id:
                return entry[1].snapshot(self._clock_ns() if at_ns is None else at_ns)
        return None

    def cancel(self) -> None:
        self.audio.session.cancel()

    def _lost(self, *_: object) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self.input.stop(reason="transport_disconnected")
        self.audio.session.reconnect()
        self._closing = asyncio.create_task(self._shutdown())

    def _participant_left(self, participant: rtc.RemoteParticipant) -> None:
        if (participant.identity == self.config.participant_identity
                and participant.sid == self.config.participant_sid):
            self._lost()

    def _track_muted(self, participant: rtc.RemoteParticipant,
                     publication: rtc.RemoteTrackPublication) -> None:
        grant = self.audio.backend.grant
        if (grant is not None and publication.sid == grant.track_sid
                and participant.identity == self.config.participant_identity
                and participant.sid == self.config.participant_sid):
            self._lost()

    def _track_unsubscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication,
                            participant: rtc.RemoteParticipant) -> None:
        self._track_muted(participant, publication)

    async def connect(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("livekit_single_connection_only")
        self._started = True
        self._handlers = [
            ("disconnected", self._lost), ("reconnecting", self._lost),
            ("participant_disconnected", self._participant_left),
            ("track_muted", self._track_muted),
            ("track_unsubscribed", self._track_unsubscribed),
        ]
        for event, handler in self._handlers:
            self.room.on(event, handler)
        self._connecting = asyncio.create_task(self._connect_room())
        try:
            done, _ = await asyncio.wait([self._connecting], timeout=self.config.connect_timeout)
            if not done:
                raise TimeoutError
            await self._connecting
            if self._closed:
                raise RuntimeError("livekit_connection_closed")
            self._connected = True
            self._pump = asyncio.create_task(self._run_output())
        except BaseException as error:
            await self.aclose()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError("livekit_connection_failed") from None

    async def _connect_room(self) -> None:
        try:
            await self.room.connect(self.config.url, self.config.token,
                                    rtc.RoomOptions(auto_subscribe=True))
        finally:
            # SDKが取消を吸収してclose後に接続しても、所有Roomを再切断する。
            if self._closed:
                await self._disconnect_owned()

    def confirm_output_ready(self, response_id: str, track_sid: str) -> bool:
        output = self._output
        if (output is None or output.response_id != response_id or output.sid != track_sid
                or not self._current(response_id)):
            return False
        output.ready.set()
        return True

    async def open_input(self, *, track_sid: str, request_id: str, revision: int) -> InputGrant:
        if not self._connected or self._closed:
            raise RuntimeError("livekit_not_connected")
        return await self.input.open(track_sid=track_sid, request_id=request_id, revision=revision)

    def _current(self, response_id: str) -> bool:
        return not self._closed and self.playback.active == response_id

    async def _retire(self) -> None:
        output, self._output = self._output, None
        if output is None:
            return
        try:
            output.stop()
            if output.sid is not None and not self._disconnecting and self.room.isconnected():
                async with asyncio.timeout(self.config.close_timeout):
                    await self.room.local_participant.unpublish_track(output.sid)
        finally:
            await output.source.aclose()

    async def _run_output(self) -> None:
        try:
            while not self._closed:
                await self.playback._wake.wait()
                self.playback._wake.clear()
                if self._output is not None and not self._current(self._output.response_id):
                    await self._retire()
                while not self._closed and (packet := self.playback.consume()) is not None:
                    self._sending = asyncio.create_task(self._send(packet))
                    try:
                        await self._sending
                    except asyncio.CancelledError:
                        if self._closed:
                            return
                    finally:
                        self._sending = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self._report("output_delivery_failed")
            self._lost()
        finally:
            await self._retire()

    async def _send(self, packet: AudioPacket) -> None:
        if not self._current(packet.response_id):
            return
        with wave.open(io.BytesIO(packet.wav), "rb") as wav:
            rate, frames = wav.getframerate(), wav.getnframes()
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or wav.getcomptype() != "NONE" or rate not in {16000, 22050, 24000, 44100, 48000}
                    or not 0 < frames <= 2_000_000):
                raise ValueError("unsupported_livekit_wav")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("truncated_livekit_wav")
        output = self._output
        if output is not None and output.response_id != packet.response_id:
            await self._retire()
            output = None
        if not self._current(packet.response_id):
            return
        if output is None:
            source = rtc.AudioSource(rate, 1, queue_size_ms=_OUTPUT_QUEUE_MS)
            try:
                track = rtc.LocalAudioTrack.create_audio_track(
                    "ds-response-v1:" + packet.response_id, source,
                )
            except BaseException:
                await source.aclose()
                raise
            output = self._output = _Output(
                packet.response_id, source, track, rate, self.audio.session.generation,
            )
            publication = await self.room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=False),
            )
            output.sid = publication.sid
            if not self._current(packet.response_id):
                output.stop()
                return
            ledger = SentAudioProgress(
                downlink_delay_ns=round(self.config.estimated_downlink_delay * 1e9),
                sdk_queue_allowance_ns=_OUTPUT_QUEUE_MS * 1_000_000,
            )
            output.scope = ledger.begin(
                response_id=packet.response_id, generation=output.generation,
                track_sid=output.sid, sample_rate=rate,
            )
            output.progress = ledger
            self._previous_progress = self._progress
            self._progress = (packet.response_id, ledger)
            self._on_track(TrackPublished(packet.response_id, output.sid))
        if output.sample_rate != rate:
            raise ValueError("livekit_response_rate_changed")
        start = output.samples
        progress, scope = output.progress, output.scope
        if progress is None or scope is None:
            raise RuntimeError("sent_audio_scope_unavailable")
        if not progress.begin_block(scope=scope, audio_sequence=packet.sequence,
                                    sample_start=start, sample_end=start + frames):
            return
        try:
            async with asyncio.timeout(self.config.output_ready_timeout):
                await output.ready.wait()
            for offset in range(0, len(pcm), (rate // 100) * 2):
                if not self._current(packet.response_id):
                    return
                chunk = pcm[offset:offset + (rate // 100) * 2]
                await output.source.capture_frame(rtc.AudioFrame(chunk, rate, 1, len(chunk) // 2))
                # native投入の成功だけを記録する。失効後の遅着は送出範囲へ足さない。
                if not self._current(packet.response_id) or self._output is not output:
                    return
                end = output.samples + len(chunk) // 2
                if not progress.record(scope=scope, audio_sequence=packet.sequence,
                                       sample_start=output.samples, sample_end=end,
                                       completed_at_ns=self._clock_ns()):
                    return
                output.samples = end
            if self._current(packet.response_id):
                # sidはpublish成功後だけ設定される。送出観測をACKへ変換しない。
                if output.sid is not None:
                    self._on_segment(SegmentSent(packet.response_id, packet.sequence, output.sid,
                                                 rate, start, output.samples))
        finally:
            if not self._current(packet.response_id):
                # SDKが取消を吸収して遅れてcaptureを完了してもqueueを再度消す。
                output.stop()

    async def _shutdown(self) -> None:
        for event, handler in self._handlers:
            self.room.off(event, handler)
        self._handlers.clear()
        # SDK connectはnative handle取得前に取消すると解放経路を失う。
        # 待機だけを制限し、所有taskは遅着後の_disconnectまで保持する。
        self.playback._wake.set()
        operations = [asyncio.create_task(self.input.aclose()),
                      asyncio.create_task(self.audio.close()),
                      asyncio.create_task(self._disconnect_room())]
        operations.extend(task for task in (self._connecting, self._pump) if task is not None)
        _, pending = await asyncio.wait(operations, timeout=self.config.close_timeout)
        if pending:
            self.audio.session.emit(Event("shutdown_pending", detail="livekit_not_drained"))
        for task in operations:
            def observed(done: asyncio.Task[None]) -> None:
                if not done.cancelled() and done.exception() is not None:
                    self._report("livekit_cleanup_failed")
            if task.done():
                observed(task)
            else:
                task.add_done_callback(observed)

    async def _disconnect_room(self) -> None:
        # 接続中のRoomでは先にunpublishの応答を受け取る。切断後には送らない。
        if self._pump is not None:
            await asyncio.wait([self._pump], timeout=self.config.close_timeout)
        self._disconnecting = True
        await self._disconnect_owned()

    async def _disconnect_owned(self) -> None:
        async with self._disconnect_lock:
            if self.room.isconnected():
                await self.room.disconnect()

    async def aclose(self) -> None:
        if self._closing is None:
            self._closed = True
            self._connected = False
            self.input.stop(reason="transport_closed")
            self.audio.session.cancel("closed")
            self._closing = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._closing)
