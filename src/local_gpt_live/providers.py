"""既存共有サービスとCoreのHTTP adapter。自動retry・fallback・本文ログなし。"""

import io
import json
import wave
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx


class ProviderError(RuntimeError):
    pass


def client(url: str, *, transport: httpx.AsyncBaseTransport | None = None,
           core: bool = False) -> httpx.AsyncClient:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("invalid_provider_url")
    if core and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("core_requires_loopback")
    return httpx.AsyncClient(base_url=url.rstrip("/"), timeout=50, transport=transport,
                             trust_env=False, follow_redirects=False)


class Whisper:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def transcribe(self, pcm: bytes) -> str:
        if not pcm or len(pcm) % 2 or len(pcm) > 960_000:
            raise ValueError("invalid_pcm")
        try:
            response = await self.http.post("/v1/transcriptions", content=pcm,
                                            headers={"Content-Type": "application/octet-stream"})
            response.raise_for_status()
            text = response.json()["text"]
            if not isinstance(text, str) or len(text) > 16_000:
                raise ValueError
            return text
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ProviderError("stt_failed") from None


class Voicevox:
    def __init__(self, http: httpx.AsyncClient, speaker_id: int) -> None:
        if type(speaker_id) is not int or speaker_id < 0:
            raise ValueError("invalid_speaker")
        self.http, self.speaker_id = http, speaker_id

    async def synthesize(self, text: str) -> bytes:
        try:
            query = await self.http.post("/audio_query",
                                         params={"text": text, "speaker": self.speaker_id})
            query.raise_for_status()
            payload = query.json()
            if not isinstance(payload, dict):
                raise ValueError
            result = await self.http.post("/synthesis", params={"speaker": self.speaker_id},
                                          json=payload)
            result.raise_for_status()
            data = result.content
            if len(data) > 4_000_000:
                raise ValueError
            with wave.open(io.BytesIO(data)) as wav:
                if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                        or wav.getnframes() == 0 or wav.getframerate() not in {16000, 22050, 24000, 44100, 48000}
                        or len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2):
                    raise ValueError
            return data
        except (httpx.HTTPError, ValueError, wave.Error, EOFError):
            raise ProviderError("tts_failed") from None


class CoreChat:
    """modelはCore登録alias。人格promptもprovider設定も音声側で作らない。"""

    def __init__(self, http: httpx.AsyncClient, model: str) -> None:
        if not model.strip():
            raise ValueError("empty_core_alias")
        self.http, self.model = http, model

    async def stream(self, text: str) -> AsyncIterator[str]:
        try:
            async with self.http.stream("POST", "/v1/chat/completions", json={
                "model": self.model, "messages": [{"role": "user", "content": text}],
                "stream": True,
            }) as response:
                response.raise_for_status()
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    raise ProviderError("core_invalid_stream")
                data: list[str] = []
                event = ""
                async for line in response.aiter_lines():
                    if len(line) > 65_536 or sum(map(len, data)) > 65_536:
                        raise ProviderError("core_stream_capacity_exceeded")
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line:
                        if event == "error":
                            raise ProviderError("core_stream_failed")
                        if not data:
                            event = ""
                            continue
                        value = "\n".join(data)
                        data, event = [], ""
                        if value == "[DONE]":
                            return
                        payload = json.loads(value)
                        if "error" in payload:
                            raise ProviderError("core_stream_failed")
                        for choice in payload.get("choices", []):
                            if choice.get("index", 0) != 0:
                                raise ProviderError("core_multiple_choices_unsupported")
                            delta = choice.get("delta", {})
                            if delta.get("tool_calls"):
                                raise ProviderError("core_tools_require_agent")
                            content = delta.get("content")
                            if content is not None:
                                if not isinstance(content, str):
                                    raise ValueError
                                yield content
                raise ProviderError("core_stream_incomplete")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ProviderError("core_failed") from None
