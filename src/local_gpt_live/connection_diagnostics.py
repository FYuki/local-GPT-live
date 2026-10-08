"""明示接続診断。入力は数量だけを観測し、出力は短いプログラム生成音。"""

import io
import math
import struct
import wave
from collections.abc import AsyncIterator

from .voice_input.pipeline import ProcessedFrame, VoiceInputPipeline


class ConnectionDiagnostics(VoiceInputPipeline):
    def __init__(self) -> None:
        self._input_samples = 0
        self._closed = False
        self._next_sample: int | None = None

    def reset(self, next_sample: int | None = None, *, quarantine: bool = False) -> None:
        if self._closed:
            raise RuntimeError("diagnostics_closed")
        self._next_sample = next_sample

    def feed(self, pcm: bytes, *, start_sample: int) -> tuple[ProcessedFrame, ...]:
        if self._closed or not pcm or len(pcm) % 2 or start_sample < 0:
            raise ValueError("invalid_diagnostic_audio")
        if self._next_sample is not None and start_sample != self._next_sample:
            raise ValueError("diagnostic_audio_gap")
        self._input_samples += len(pcm) // 2
        self._next_sample = start_sample + len(pcm) // 2
        return ()

    def snapshot(self) -> dict[str, int]:
        return {"input_samples": self._input_samples}

    def close(self) -> None:
        self._closed = True


class DiagnosticTranscriber:
    async def transcribe(self, pcm: bytes) -> str:
        raise RuntimeError("diagnostics_has_no_transcription")


class DiagnosticCore:
    async def stream(self, text: str) -> AsyncIterator[str]:
        yield "接続診断音。"


class DiagnosticSynthesizer:
    async def synthesize(self, text: str) -> bytes:
        rate, samples = 16000, 4800
        pcm = b"".join(struct.pack("<h", round(
            3000 * math.sin(2 * math.pi * 440 * index / rate)
            * min(1, index / 160, (samples - index) / 160)
        )) for index in range(samples))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(pcm)
        return buffer.getvalue()
