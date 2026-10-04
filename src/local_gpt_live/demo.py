"""実サービス・録音を使わないCLI/APIの合成イベントハーネス。"""

import argparse
import asyncio
import io
import json
import struct
import wave
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .input import AudioInput
from .playback import Playback
from .session import Event, VoiceSession
from .voice_input.pipeline import VoiceInputPipeline


def synthetic_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(3200))
    return output.getvalue()


class FixtureStt:
    def __init__(self) -> None:
        self.transcripts: dict[bytes, str] = {}
        self.calls = 0

    async def transcribe(self, pcm: bytes) -> str:
        self.calls += 1
        return self.transcripts[pcm]


class FixtureCore:
    async def stream(self, text: str) -> AsyncIterator[str]:
        if text == "timeout":
            await asyncio.Event().wait()
        yield "合成fixtureの"
        yield "応答です。"


class FixtureTts:
    async def synthesize(self, text: str) -> bytes:
        return synthetic_wav()


async def run_scenario(events: list[dict[str, Any]]) -> list[Event]:
    """公開Python API。入力はリポジトリ同梱の架空fixtureを想定する。"""
    observed: list[Event] = []
    stt = FixtureStt()
    session = VoiceSession(stt, FixtureCore(), FixtureTts(), Playback(),
                           emit=observed.append, response_timeout=0.05)
    ingress = AudioInput(session, VoiceInputPipeline())
    revision = 1
    grant = await ingress.open(track_sid="fixture-1", request_id="open-1", revision=revision)
    try:
        for index, event in enumerate(events):
            kind = event["kind"]
            if kind == "text":
                revision += 1
                ingress.submit_text(event["text"], revision=revision)
            elif kind == "audio":
                # 正式発話終了の合成イベント。実STT/VAD認識の合格ではない。
                pcm = struct.pack("<h", 1000 + index) * 1600
                stt.transcripts[pcm] = event["text"]
                session.submit_audio(pcm, generation=session.generation,
                                     overlap=session.active)
            elif kind == "silence":
                for frame in range(24):
                    await ingress.backend.receive(bytes(3072), start_sample=frame * 1536,
                                                  grant=grant)
            elif kind == "settle":
                await session.drain()
            elif kind == "playback":
                response_id = session.active
                while (packet := session.playback.consume()) is not None:
                    # 合成端末の区間完了ACK。実ブラウザの出力確認とは別のfixture。
                    if not session.acknowledge_playback(packet.response_id, packet.sequence):
                        raise RuntimeError("fixture_playback_ack_rejected")
                if response_id is not None:
                    session.playback_completed(response_id)
            elif kind == "cancel":
                session.cancel()
            elif kind == "reconnect":
                revision += 1
                ingress.reconnect(revision=revision)
                revision += 1
                grant = await ingress.open(track_sid=f"fixture-{revision}",
                                            request_id=f"open-{revision}", revision=revision)
            else:
                raise ValueError("unknown_fixture_event")
        await session.drain()
        return observed.copy()
    finally:
        await ingress.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="合成イベント検証。マイク・GPUは使いません。")
    parser.add_argument("--fixture", type=Path,
                        default=Path(__file__).with_name("fixtures") / "scenarios.json")
    args = parser.parse_args()
    fixtures = json.loads(args.fixture.read_text(encoding="utf-8"))
    for scenario in fixtures:
        events = asyncio.run(run_scenario(scenario["events"]))
        actual = [event.kind for event in events]
        for expected in scenario["expect"]:
            if expected not in actual:
                raise AssertionError(f"{scenario['name']}: missing {expected}")
        for forbidden in scenario.get("forbid", []):
            if forbidden in actual:
                raise AssertionError(f"{scenario['name']}: unexpected {forbidden}")
        print(json.dumps({"scenario": scenario["name"], "status": "pass",
                          "events": [asdict(event) for event in events]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
