"""既存Irodori HTTP契約。登録済みvoiceを参照し、声資産は所有しない。"""

import asyncio
import io
import math
import re
import wave
from dataclasses import dataclass

import httpx

from .providers import ProviderError


@dataclass(frozen=True)
class IrodoriVoice:
    voice_id: str
    caption: str
    seed: int
    speed: float = 1.0
    num_steps: int = 40

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.voice_id)
                or self.voice_id.lower() in {"none", "no_ref", "no-ref", "null", "text-only"}
                or len(self.caption) > 4096 or type(self.seed) is not int
                or not 0 <= self.seed <= 2**32 - 1 or not math.isfinite(self.speed)
                or not .25 <= self.speed <= 4 or type(self.num_steps) is not int
                or not 1 <= self.num_steps <= 100):
            raise ValueError("invalid_irodori_voice")


class Irodori:
    def __init__(self, http: httpx.AsyncClient, voice: IrodoriVoice,
                 *, environment: str = "test") -> None:
        if environment not in {"dev", "test"}:
            raise ValueError("development_adapter_requires_dev_or_test")
        self.http, self.voice, self.environment = http, voice, environment

    async def prepare(self) -> None:
        try:
            async with asyncio.timeout(5):
                ready = await self.http.get("/health/ready")
                ready.raise_for_status()
                voices = await self.http.get("/v1/audio/voices")
                voices.raise_for_status()
                if ready.json() != {"status": "ready"}:
                    raise ValueError
                if not any(item["id"] == self.voice.voice_id for item in voices.json()["data"]):
                    raise ValueError
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise ProviderError("tts_not_ready") from None

    async def synthesize(self, text: str) -> bytes:
        voice = self.voice
        # キャラクター固有の読み辞書は呼出側の音声設定へ分離する。
        payload = {
            "model": "irodori-tts", "input": text, "voice": voice.voice_id,
            "response_format": "wav", "speed": voice.speed,
            "irodori": {"caption": voice.caption, "seed": voice.seed,
                        "num_steps": voice.num_steps, "chunking_enabled": False},
        }
        try:
            async with asyncio.timeout(45):
                async with self.http.stream("POST", "/v1/audio/speech", json=payload,
                                            headers={"X-DS-Environment": self.environment}) as response:
                    if response.status_code != 200:
                        raise ProviderError("tts_service_failed")
                    if response.headers.get("content-type", "").split(";")[0] != "audio/wav":
                        raise ValueError
                    audio = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(audio) + len(chunk) > 4_000_000:
                            raise ValueError
                        audio.extend(chunk)
                with wave.open(io.BytesIO(audio)) as wav:
                    pcm = wav.readframes(wav.getnframes())
                    if (wav.getcomptype() != "NONE" or wav.getnchannels() != 1
                            or wav.getsampwidth() != 2 or wav.getframerate() != 48_000
                            or not pcm or len(pcm) != wav.getnframes() * 2 or not any(pcm)):
                        raise ValueError
                return bytes(audio)
        except (httpx.HTTPError, TimeoutError, ValueError, wave.Error, EOFError, OSError):
            raise ProviderError("tts_failed") from None
