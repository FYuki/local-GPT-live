"""BEが所有する音声入力世代・正式utteranceと、端末入力の境界。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from local_gpt_live.voice_input.detector import Detection
from local_gpt_live.voice_input.pipeline import (
    AudioInputFault,
    ProcessedFrame,
    VoiceInputPipeline,
    VoiceInputWorker,
)

MAX_INPUT_TRACKS = 256


@dataclass(frozen=True)
class InputGrant:
    track_sid: str
    input_generation: int
    request_id: str
    input_revision: int


@dataclass(frozen=True)
class SpeechBoundary:
    utterance_id: str
    grant: InputGrant
    detection: Detection


class BackendVoiceInput:
    """認証済みtrackだけをopenする責任は呼出側transportにある。"""

    def __init__(
        self,
        pipeline: VoiceInputPipeline,
        *,
        on_frame: Callable[[ProcessedFrame], None],
        on_started: Callable[[SpeechBoundary], None],
        on_stopped: Callable[[SpeechBoundary], Awaitable[None]],
        on_discarded: Callable[[str | None, str], None],
        new_utterance_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._worker = VoiceInputWorker(pipeline)
        self._on_frame = on_frame
        self._on_started = on_started
        self._on_stopped = on_stopped
        self._on_discarded = on_discarded
        self._new_id = new_utterance_id
        self._grant: InputGrant | None = None
        self._generation = 0
        self._revision = 0
        self._used_tracks: set[str] = set()
        self._active_utterance: str | None = None
        self._closed = False

    @property
    def grant(self) -> InputGrant | None:
        return self._grant

    @property
    def revision(self) -> int:
        return self._revision

    def suppress(self, *, reason: str, input_revision: int | None = None) -> bool:
        if self._closed:
            return False
        if input_revision is not None:
            if type(input_revision) is not int or input_revision <= self._revision:
                return False
            self._revision = input_revision
        self._generation += 1
        self._grant = None
        if self._active_utterance is not None:
            utterance_id, self._active_utterance = self._active_utterance, None
            self._on_discarded(utterance_id, reason)
        return True

    async def open(
        self,
        *,
        track_sid: str,
        request_id: str,
        input_revision: int,
    ) -> InputGrant:
        if self._closed:
            raise AudioInputFault("audio_input_closed")
        if (
            not track_sid
            or not request_id
            or type(input_revision) is not int
            or input_revision <= 0
        ):
            raise AudioInputFault("invalid_input_request")
        grant = self._grant
        if grant is not None and (track_sid, request_id, input_revision) == (
            grant.track_sid,
            grant.request_id,
            grant.input_revision,
        ):
            return grant
        if input_revision <= self._revision:
            raise AudioInputFault("stale_input_request")
        # 抑止中に送られた旧trackの遅着PCMを、新世代として受け入れない。
        if track_sid in self._used_tracks:
            raise AudioInputFault("new_microphone_track_required")
        if len(self._used_tracks) >= MAX_INPUT_TRACKS:
            raise AudioInputFault("input_capacity_exceeded")
        self.suppress(reason="input_replaced", input_revision=input_revision)
        self._used_tracks.add(track_sid)
        generation = self._generation
        try:
            await self._worker.reset()
        except BaseException:
            if self._generation == generation:
                self.suppress(reason="input_open_failed")
            raise
        if self._closed or self._generation != generation:
            raise AudioInputFault("stale_input_request")
        grant = InputGrant(track_sid, generation, request_id, input_revision)
        self._grant = grant
        return grant

    async def receive(
        self, pcm: bytes, *, start_sample: int, grant: InputGrant
    ) -> None:
        if self._closed or self._grant is not grant:
            return
        try:
            frames = await self._worker.process(pcm, start_sample=start_sample)
        except AudioInputFault as error:
            if self._grant is not grant:
                return
            utterance_id, self._active_utterance = self._active_utterance, None
            self._on_discarded(utterance_id, error.code)
            # 欠落した発話の語尾は静音境界を確認するまで採用しない。
            try:
                await self._worker.reset(quarantine=True)
            except AudioInputFault:
                if self._grant is not grant:
                    return
                self.suppress(reason="input_closed")
                raise
            return
        if self._grant is not grant:
            return
        for frame in frames:
            if self._grant is not grant:
                return
            self._on_frame(frame)
            for detection in frame.detections:
                if detection.kind == "confirmed":
                    utterance_id = self._new_id()
                    self._active_utterance = utterance_id
                    self._on_started(SpeechBoundary(utterance_id, grant, detection))
                elif detection.kind == "ended" and self._active_utterance is not None:
                    utterance_id, self._active_utterance = self._active_utterance, None
                    # 確定済み区間はfocus抑止から分離する。text優先の取消はCoreが担う。
                    await self._on_stopped(
                        SpeechBoundary(utterance_id, grant, detection)
                    )

    async def close(self) -> None:
        if self._closed:
            return
        self.suppress(reason="input_closed")
        self._closed = True
        await self._worker.close()
