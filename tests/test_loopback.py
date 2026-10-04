"""HTTP切断を実loopback socketで確認する。GPU/外部サービスは使わない。"""

import asyncio
import contextlib
import json

from local_gpt_live.demo import FixtureStt, FixtureTts
from local_gpt_live.playback import Playback
from local_gpt_live.providers import CoreChat, client
from local_gpt_live.session import VoiceSession


async def test_cancel_closes_actual_core_socket_and_stops_output():
    arrived, disconnected = asyncio.Event(), asyncio.Event()
    connections = []

    async def core_socket(reader, writer):
        connections.append(writer)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            body = json.loads(await reader.readexactly(length))
            assert body["stream"] is True
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            chunk = b'data: {"choices":[{"delta":{"content":"hello."}}]}\n\n'
            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            await writer.drain()
            arrived.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(core_socket, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with client(f"http://127.0.0.1:{port}", core=True) as http:
            playback = Playback()
            session = VoiceSession(FixtureStt(), CoreChat(http, "fixture"), FixtureTts(), playback)
            try:
                session.submit_text("合成socket入力")
                await asyncio.wait_for(arrived.wait(), 2)
                session.cancel()
                await asyncio.wait_for(disconnected.wait(), 2)
                assert playback.consume() is None and session.active is None
            finally:
                await session.close()
    finally:
        server.close()
        await server.wait_closed()
        for writer in connections:
            writer.close()
