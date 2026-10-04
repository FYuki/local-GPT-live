import io
import json
import wave

import httpx
import pytest

from local_gpt_live.irodori import Irodori, IrodoriVoice
from local_gpt_live.providers import ProviderError, client


def wav_fixture():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x01\x00" * 4800)
    return output.getvalue()


async def test_registered_voice_contract_without_bundled_voice_assets():
    audio = wav_fixture()

    def handler(request):
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"data": [{"id": "fixture-voice"}]})
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["X-DS-Environment"] == "test"
        body = json.loads(request.content)
        assert body["voice"] == "fixture-voice"
        assert body["irodori"] == {"caption": "fixture", "seed": 42,
                                   "num_steps": 40, "chunking_enabled": False}
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=audio)

    async with client("http://localhost", transport=httpx.MockTransport(handler)) as http:
        adapter = Irodori(http, IrodoriVoice("fixture-voice", "fixture", 42))
        await adapter.prepare()
        assert await adapter.synthesize("架空の入力") == audio


async def test_missing_voice_does_not_fallback():
    transport = httpx.MockTransport(lambda r: httpx.Response(200,
        json={"status": "ready"} if r.url.path == "/health/ready" else {"data": []}))
    async with client("http://localhost", transport=transport) as http:
        with pytest.raises(ProviderError, match="tts_not_ready"):
            await Irodori(http, IrodoriVoice("fixture", "", 0)).prepare()


@pytest.mark.parametrize("voice", ["none", "no-ref", "../private", ""])
def test_reference_voice_is_required(voice):
    with pytest.raises(ValueError):
        IrodoriVoice(voice, "", 0)
