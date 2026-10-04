"""連続PCMのframe化・idle reset・発話区間と容量をSessionごとに管理する。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from local_gpt_live.voice_input.detector import Detection, ShortSpeechEvidence, UtteranceDetector
from local_gpt_live.voice_input.models import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    ShortSpeechAnalyzer,
    SileroLegacy,
)

MAX_UTTERANCE_SAMPLES = 30 * SAMPLE_RATE
MAX_CHUNK_BYTES = 2 * SAMPLE_RATE
MAX_PENDING_FRAMES = 16


class AudioInputFault(ValueError):
    """呼出側は該当発話を破棄し、codeに応じた利用者向け通知を行う。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProcessedFrame:
    start_sample: int
    end_sample: int
    pcm: bytes
    probability: float
    evidence: ShortSpeechEvidence
    detections: tuple[Detection, ...]
    model_reset: bool


class VoiceInputPipeline:
    def __init__(self) -> None:
        self._model = SileroLegacy()
        self._secondary = ShortSpeechAnalyzer()
        self._detector = UtteranceDetector()
        self._closed = False
        self.reset()

    def reset(
        self, next_sample: int | None = None, *, quarantine: bool = False
    ) -> None:
        if self._closed:
            raise RuntimeError("vad_closed")
        self._model.reset()
        self._secondary.reset()
        self._detector.reset()
        self._pending = bytearray()
        self._next_sample = next_sample
        self._processed_sample = next_sample or 0
        self._quiet_samples = 0
        self._since_reset = 4096
        self._utterance_start: int | None = None
        self._quarantine = quarantine

    @property
    def buffered_bytes(self) -> int:
        return len(self._pending)

    def feed(self, pcm: bytes, *, start_sample: int) -> tuple[ProcessedFrame, ...]:
        if self._closed:
            raise RuntimeError("vad_closed")
        if (
            not isinstance(pcm, bytes)
            or len(pcm) % 2
            or not pcm
            or len(pcm) > MAX_CHUNK_BYTES
            or type(start_sample) is not int
            or start_sample < 0
        ):
            self.reset(quarantine=True)
            raise AudioInputFault("invalid_audio_frame")
        if self._next_sample is None:
            self._next_sample = self._processed_sample = start_sample
        if start_sample != self._next_sample:
            # 欠落後の語尾だけを別発話へ変えない。静音を確認するまで採用を止める。
            self.reset(start_sample + len(pcm) // 2, quarantine=True)
            raise AudioInputFault("audio_gap")
        self._next_sample += len(pcm) // 2
        self._pending.extend(pcm)
        frames: list[ProcessedFrame] = []
        while len(self._pending) >= FRAME_SAMPLES * 2:
            data = bytes(self._pending[: FRAME_SAMPLES * 2])
            del self._pending[: FRAME_SAMPLES * 2]
            start = self._processed_sample
            self._processed_sample += FRAME_SAMPLES
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / np.float32(
                32768
            )
            quiet = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) < 0.001
            self._quiet_samples = (
                min(11200, self._quiet_samples + FRAME_SAMPLES) if quiet else 0
            )
            self._since_reset = min(4096, self._since_reset + FRAME_SAMPLES)
            reset = self._quiet_samples >= 11200 and self._since_reset >= 4096
            if reset:
                self._model.reset()
                self._secondary.reset()
                self._since_reset = 0
            if self._quarantine:
                if self._quiet_samples >= 11200:
                    self._quarantine = False
                # 再開の静音frameは境界形成へ使わない。
                continue
            probability = self._model.process(samples)
            evidence = self._secondary.process(samples)
            detections = self._detector.process(
                samples, probability, self._processed_sample, evidence
            )
            for event in detections:
                if event.kind == "confirmed":
                    self._utterance_start = event.started_sample
                elif event.kind in {"ended", "misfire"}:
                    self._utterance_start = None
            if (
                self._utterance_start is not None
                and self._processed_sample - self._utterance_start
                > MAX_UTTERANCE_SAMPLES
            ):
                # 同じ入力chunkの残りも捨てて、上限超過の末尾を次発話にしない。
                self.reset(self._next_sample, quarantine=True)
                raise AudioInputFault("input_capacity_exceeded")
            frames.append(
                ProcessedFrame(
                    start,
                    self._processed_sample,
                    data,
                    probability,
                    evidence,
                    detections,
                    reset,
                )
            )
        return tuple(frames)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._model.reset()
        self._detector.reset()
        self._secondary.close()


class VoiceInputWorker:
    """推論をイベントループ外で直列化し、音声量・遅延結果の採用を制限する。"""

    def __init__(self, pipeline: VoiceInputPipeline) -> None:
        self._pipeline = pipeline
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voice-vad"
        )
        self._pending_samples = 0
        self._pending_jobs = 0
        self._epoch = 0
        self._closed = False
        self._needs_reset = False
        self._reset_lock = asyncio.Lock()

    def _fault(self, code: str) -> AudioInputFault:
        self._epoch += 1
        self._needs_reset = True
        return AudioInputFault(code)

    async def process(
        self, pcm: bytes, *, start_sample: int
    ) -> tuple[ProcessedFrame, ...]:
        if self._closed:
            raise RuntimeError("vad_closed")
        if self._needs_reset:
            raise AudioInputFault("vad_reset_required")
        if (
            not isinstance(pcm, bytes)
            or not pcm
            or len(pcm) % 2
            or len(pcm) > MAX_CHUNK_BYTES
            or type(start_sample) is not int
            or start_sample < 0
        ):
            raise self._fault("invalid_audio_frame")
        samples = len(pcm) // 2
        if (
            self._pending_jobs >= MAX_PENDING_FRAMES
            or self._pending_samples + samples > MAX_PENDING_FRAMES * FRAME_SAMPLES
        ):
            raise self._fault("vad_backlog_exceeded")
        epoch = self._epoch
        self._pending_samples += samples
        self._pending_jobs += 1

        def run() -> tuple[ProcessedFrame, ...]:
            # resetより前の待機入力は推論も行わない。
            if self._closed or epoch != self._epoch:
                return ()
            return self._pipeline.feed(pcm, start_sample=start_sample)

        future = asyncio.get_running_loop().run_in_executor(self._executor, run)

        def completed(done: asyncio.Future[tuple[ProcessedFrame, ...]]) -> None:
            # 呼出taskのcancelで実行中の音声量を解放しない。
            self._pending_samples -= samples
            self._pending_jobs -= 1
            if not done.cancelled():
                error = done.exception()
                if error is not None and epoch == self._epoch:
                    self._fault("vad_processing_failed")

        future.add_done_callback(completed)
        try:
            frames = await asyncio.shield(future)
            return frames if not self._closed and epoch == self._epoch else ()
        except asyncio.CancelledError:
            if not self._closed and epoch == self._epoch:
                self._fault("vad_processing_cancelled")
            raise
        except AudioInputFault:
            raise
        except Exception as error:
            # native推論の例外をtransport全体の障害へ拡散させない。
            # completedが失敗epochを無効化し、呼出側が発話破棄とresetを行う。
            raise AudioInputFault("vad_processing_failed") from error

    async def reset(
        self, next_sample: int | None = None, *, quarantine: bool = False
    ) -> None:
        if self._closed:
            return
        self._epoch += 1
        epoch = self._epoch
        self._needs_reset = True
        async with self._reset_lock:
            if self._closed:
                return
            try:
                await asyncio.shield(
                    asyncio.get_running_loop().run_in_executor(
                        self._executor,
                        lambda: self._pipeline.reset(next_sample, quarantine=quarantine),
                    )
                )
            except Exception as error:
                # 部分的にresetされた状態を利用可能とは扱わない。
                raise AudioInputFault("vad_unavailable") from error
            if epoch == self._epoch:
                self._needs_reset = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._epoch += 1
        try:
            await asyncio.shield(
                asyncio.get_running_loop().run_in_executor(
                    self._executor, self._pipeline.close
                )
            )
        finally:
            await asyncio.to_thread(
                self._executor.shutdown, wait=True, cancel_futures=True
            )
