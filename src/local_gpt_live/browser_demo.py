"""取得済みの接続設定で既存hostを組み立てるローカル入口。"""

import argparse
import asyncio
import getpass
import json
import signal
import sys
from dataclasses import dataclass
from typing import Literal, TextIO
from uuid import uuid4

import httpx

from .connection_diagnostics import (
    ConnectionDiagnostics,
    DiagnosticCore,
    DiagnosticSynthesizer,
    DiagnosticTranscriber,
)
from .input import AudioInput
from .livekit_host import LiveKitHost
from .host_rpc import identifier
from .livekit_transport import LiveKitConfig, LiveKitPlayback, LiveKitTransport
from .providers import CoreChat, Voicevox, Whisper, client, validate_provider_url
from .session import Core, Synthesizer, Transcriber, VoiceSession
from .voice_input.pipeline import VoiceInputPipeline


@dataclass
class BrowserDemo:
    host: LiveKitHost
    mode: str
    diagnostics: ConnectionDiagnostics | None
    clients: tuple[httpx.AsyncClient, ...]

    async def aclose(self) -> None:
        try:
            await self.host.aclose()
        finally:
            for http in self.clients:
                await http.aclose()


def create_demo(
    *, url: str, token: str, participant_identity: str, participant_sid: str,
    session_id: str, mode: str, stt_url: str | None, core_url: str | None,
    tts_url: str | None, core_alias: str | None, speaker_id: int | None,
) -> BrowserDemo:
    if not identifier(session_id):
        raise ValueError("invalid_host_session")
    config = LiveKitConfig(url, token, participant_identity, participant_sid)
    diagnostics = None
    clients: tuple[httpx.AsyncClient, ...] = ()
    stt: Transcriber
    core: Core
    tts: Synthesizer
    pipeline: VoiceInputPipeline
    if mode == "conversation":
        if not stt_url or not core_url or not tts_url or not core_alias or speaker_id is None:
            raise ValueError("conversation_settings_required")
        if not core_alias.strip() or type(speaker_id) is not int or speaker_id < 0:
            raise ValueError("invalid_conversation_settings")
        # 構築前にURLを検証し、途中で未所有のHTTP clientを残さない。
        validate_provider_url(stt_url)
        validate_provider_url(core_url, core=True)
        validate_provider_url(tts_url)
        pipeline = VoiceInputPipeline()
        clients = (client(stt_url), client(core_url, core=True), client(tts_url))
        stt, core, tts = Whisper(clients[0]), CoreChat(clients[1], core_alias), Voicevox(clients[2], speaker_id)
    elif mode == "diagnostics":
        diagnostics = ConnectionDiagnostics()
        pipeline = diagnostics
        stt, core, tts = DiagnosticTranscriber(), DiagnosticCore(), DiagnosticSynthesizer()
    else:
        raise ValueError("invalid_demo_mode")
    session = VoiceSession(stt, core, tts, LiveKitPlayback())
    audio = AudioInput(session, pipeline)
    host = LiveKitHost(LiveKitTransport(audio, config), session_id=session_id)
    return BrowserDemo(host, mode, diagnostics, clients)


async def wait_for_start(stopped: asyncio.Event, stream: TextIO) -> bool:
    loop = asyncio.get_running_loop()
    entered: asyncio.Future[bool] = loop.create_future()
    def read() -> None:
        if not entered.done():
            entered.set_result(bool(stream.readline()))
    interrupted = asyncio.create_task(stopped.wait())
    loop.add_reader(stream.fileno(), read)
    try:
        waiting: list[asyncio.Future[bool] | asyncio.Task[Literal[True]]] = [entered, interrupted]
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        return not stopped.is_set() and entered.done() and entered.result()
    finally:
        loop.remove_reader(stream.fileno())
        interrupted.cancel()
        await asyncio.gather(interrupted, return_exceptions=True)
        if not entered.done():
            entered.cancel()


async def serve(args: argparse.Namespace, token: str) -> None:
    app = create_demo(
        url=args.url, token=token, participant_identity=args.participant_identity,
        participant_sid=args.participant_sid, session_id=args.session_id, mode=args.mode,
        stt_url=args.stt_url, core_url=args.core_url, tts_url=args.tts_url,
        core_alias=args.core_alias, speaker_id=args.speaker_id,
    )
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stopped.set)
    try:
        print(json.dumps({"mode": app.mode, "session_id": app.host.session_id,
                          "connection_id": app.host.connection_id,
                          "host_identity": args.host_identity}, ensure_ascii=False))
        print("画面へ上記設定を渡し、ブラウザの接続・通知待受後にEnterでhostを接続: ", flush=True)
        if not await wait_for_start(stopped, sys.stdin):
            return
        await app.host.connect()
        if app.host.transport.room.local_participant.identity != args.host_identity:
            raise ValueError("host_identity_mismatch")
        print("host接続済み。画面へhost SIDを設定してください。終了はCtrl+C。")
        print("host SID: " + app.host.transport.room.local_participant.sid)
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=1)
            except TimeoutError:
                if app.diagnostics is not None:
                    print(json.dumps(app.diagnostics.snapshot()))
    finally:
        await app.aclose()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)


def main() -> None:
    parser = argparse.ArgumentParser(description="既存LiveKitの最小ブラウザhost。診断と通常会話を区別します。")
    parser.add_argument("--url", required=True)
    parser.add_argument("--participant-identity", required=True)
    parser.add_argument("--participant-sid", required=True)
    parser.add_argument("--host-identity", required=True)
    parser.add_argument("--session-id", default=uuid4().hex)
    parser.add_argument("--mode", choices=("conversation", "diagnostics"), required=True)
    for name in ("stt-url", "core-url", "tts-url", "core-alias"):
        parser.add_argument("--" + name)
    parser.add_argument("--speaker-id", type=int)
    args = parser.parse_args()
    token = getpass.getpass("取得済みhostトークン（非表示）: ")
    try:
        asyncio.run(serve(args, token))
    except (Exception, KeyboardInterrupt):
        parser.exit(1, "hostを終了しました。設定・接続を確認してください。\n")


if __name__ == "__main__":
    main()
