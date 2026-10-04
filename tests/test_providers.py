import json

import httpx
import pytest

from local_gpt_live.demo import synthetic_wav
from local_gpt_live.providers import CoreChat, ProviderError, Voicevox, Whisper, client


async def test_http_contract_stt_core_tts():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path == "/v1/transcriptions":
            assert request.content == b"\0\0"
            return httpx.Response(200, json={"text": "合成入力"})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            assert body == {"model": "fixture", "stream": True,
                            "messages": [{"role": "user", "content": "合成入力"}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text='data: {"choices":[{"delta":{"content":"応答。"}}]}\n\ndata: [DONE]\n\n')
        if request.url.path == "/audio_query":
            assert request.url.params["speaker"] == "1"
            return httpx.Response(200, json={"speedScale": 1})
        assert request.url.path == "/synthesis"
        assert json.loads(request.content) == {"speedScale": 1}
        return httpx.Response(200, content=synthetic_wav())

    async with client("http://127.0.0.1:9999", transport=httpx.MockTransport(handler), core=True) as http:
        text = await Whisper(http).transcribe(b"\0\0")
        chunks = [chunk async for chunk in CoreChat(http, "fixture").stream(text)]
        assert chunks == ["応答。"]
        assert await Voicevox(http, 1).synthesize(chunks[0]) == synthetic_wav()
    assert len(requests) == 4


@pytest.mark.parametrize("body", [
    'event: error\ndata: {"error":{"message":"private upstream"}}\n\n',
    'data: {"choices":[]}\n\n',
    'data: not-json\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n',
])
async def test_core_rejects_error_eof_invalid_and_tools(body):
    transport = httpx.MockTransport(lambda r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, text=body))
    async with client("http://localhost", transport=transport) as http:
        with pytest.raises(ProviderError) as caught:
            _ = [chunk async for chunk in CoreChat(http, "fixture").stream("test")]
        assert "private upstream" not in str(caught.value)


async def test_stream_early_close_closes_http_body():
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            yield b'data: [DONE]\n\n'

        async def aclose(self):
            self.closed = True

    body = Body()
    transport = httpx.MockTransport(lambda r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, stream=body))
    async with client("http://localhost", transport=transport) as http:
        stream = CoreChat(http, "fixture").stream("test")
        assert await anext(stream) == "first"
        await stream.aclose()
        assert body.closed


@pytest.mark.parametrize("status", [429, 500, 504])
async def test_provider_failure_is_sanitized_without_fallback(status):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text="private data"))
    async with client("http://localhost", transport=transport) as http:
        with pytest.raises(ProviderError, match="stt_failed"):
            await Whisper(http).transcribe(b"aa")
        with pytest.raises(ProviderError, match="tts_failed"):
            await Voicevox(http, 1).synthesize("private query")


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1@evil.test", "http://localhost?key=x"])
def test_core_endpoint_boundary(url):
    with pytest.raises(ValueError):
        client(url, core=True)
