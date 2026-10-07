"""移植したBackend VADを、容量制限付き音声会話入力へ接続する。"""

import asyncio

from .session import Event, VoiceSession
from .stt_audio import SttSignalSpan
from .voice_input.pipeline import ProcessedFrame, VoiceInputPipeline
from .voice_input.session import BackendVoiceInput, InputGrant, SpeechBoundary


class AudioInput:
    def __init__(self, session: VoiceSession, pipeline: VoiceInputPipeline) -> None:
        self.session = session
        self._preroll = bytearray()
        self._capture: bytearray | None = None
        self._generation = 0
        self._overlap: str | None = None
        self._preview_task: asyncio.Task[None] | None = None
        self._preview_signal = SttSignalSpan()
        self._preview_attempts = 0
        self._preview_last_samples = 0
        self._preview_complete = False
        self.backend = BackendVoiceInput(
            pipeline, on_frame=self._frame, on_started=self._started,
            on_stopped=self._stopped, on_discarded=self._discarded,
        )

    async def open(self, *, track_sid: str, request_id: str, revision: int) -> InputGrant:
        old = self.backend.grant
        grant = await self.backend.open(track_sid=track_sid, request_id=request_id,
                                        input_revision=revision)
        if grant is not old:
            self._capture = None
            self._preroll.clear()
        return grant

    def _frame(self, frame: ProcessedFrame) -> None:
        self._preroll.extend(frame.pcm)
        del self._preroll[:-64_000]
        if self._capture is not None:
            self._capture.extend(frame.pcm)
            if len(self._capture) > 960_000:
                self._capture = None
                self.session.emit(Event("input_rejected", detail="input_capacity_exceeded"))
            else:
                self._consider_preview()

    def _started(self, boundary: SpeechBoundary) -> None:
        self._capture = bytearray(self._preroll)
        self._generation = self.session.generation
        self._overlap = self.session.current_output_response()
        self._preview_signal = SttSignalSpan()
        self._preview_attempts = self._preview_last_samples = 0
        self._preview_complete = False
        self.session.emit(Event("speech_started", self._overlap))
        self._consider_preview()

    def _consider_preview(self) -> None:
        capture, overlap = self._capture, self._overlap
        if (capture is None or overlap is None or self._preview_complete
                or self._preview_attempts >= 3 or not self.session.can_preview
                or (self._preview_task is not None and not self._preview_task.done())):
            return
        samples = self._preview_signal.sample_count(capture)
        if samples - self._preview_last_samples < 12_800:
            return
        self._preview_attempts += 1
        self._preview_last_samples = samples
        generation = self._generation
        pcm = bytes(capture)

        async def run() -> None:
            taken = await self.session.preview(
                pcm, generation=generation, overlap=overlap,
                is_current=lambda: self._capture is capture,
            )
            if self._capture is capture:
                self._preview_complete = taken

        self._preview_task = asyncio.create_task(run())

    async def _stopped(self, boundary: SpeechBoundary) -> None:
        capture, self._capture = self._capture, None
        self.session.emit(Event("speech_stopped", self._overlap))
        if capture is not None:
            self.session.submit_audio(bytes(capture), generation=self._generation,
                                      overlap=self._overlap)

    def _discarded(self, utterance: str | None, reason: str) -> None:
        self._capture = None
        self._preroll.clear()
        self.session.emit(Event("input_discarded", detail=reason))

    def suppress(self, *, revision: int, reason: str = "muted") -> bool:
        changed = self.backend.suppress(reason=reason, input_revision=revision)
        if changed:
            self._capture = None
            self._preroll.clear()
        return changed

    def reconnect(self, *, revision: int) -> None:
        if not self.suppress(revision=revision, reason="reconnect"):
            raise ValueError("stale_input_request")
        self.session.reconnect()

    def submit_text(self, text: str, *, revision: int) -> str:
        if not self.suppress(revision=revision, reason="text_priority"):
            raise ValueError("stale_input_request")
        return self.session.submit_text(text)

    async def close(self) -> None:
        self._capture = None
        self._preroll.clear()
        if self._preview_task is not None and not self._preview_task.done():
            self._preview_task.cancel()
            _, pending = await asyncio.wait([self._preview_task], timeout=1)
            if pending:
                self.session.emit(Event("shutdown_pending", detail="preview_not_drained"))
        await self.backend.close()
        await self.session.close()
