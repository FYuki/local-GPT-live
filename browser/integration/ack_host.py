"""Node との JSON-lines 合成テスト専用 host。ネットワーク・録音・推論を使わない。"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
from typing import Any
import wave

# 作業ディレクトリや editable install に依存せず、この統合 checkout を使う。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from local_gpt_live.demo import FixtureStt, FixtureTts  # noqa: E402
from local_gpt_live.playback import Playback  # noqa: E402
from local_gpt_live.playback_ack_transport import PlaybackAckTransport  # noqa: E402
from local_gpt_live.session import Event, VoiceSession  # noqa: E402


class SyntheticCore:
    def __init__(self) -> None:
        self.count = 0
        self.hold = False
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    def prepare(self, count: int, hold: bool) -> None:
        self.count, self.hold = count, hold
        self.ready, self.release = asyncio.Event(), asyncio.Event()

    async def stream(self, text: str):
        count, hold, ready, release = self.count, self.hold, self.ready, self.release
        for _ in range(count):
            yield "合成テスト区間。"
        ready.set()
        if hold:
            await release.wait()


class Host:
    def __init__(self) -> None:
        self.core = SyntheticCore()
        self.completed_count = 0
        self.session = VoiceSession(FixtureStt(), self.core, FixtureTts(), Playback(),
                                    emit=self._event)
        self.receiver = PlaybackAckTransport(self.session, "browser", "participant")

    def _event(self, event: Event) -> None:
        if event.kind == "playback_completed":
            self.completed_count += 1

    def status(self) -> dict[str, object]:
        return {
            "active": self.session.active,
            "generated": self.session.generated,
            "confirmed_sequence": self.session.playback.confirmed_sequence,
            "all_confirmed": self.session.playback.all_confirmed,
            "completed_count": self.completed_count,
        }

    async def start(self, command: dict[str, Any]) -> dict[str, object]:
        count, hold = command.get("count", 2), command.get("hold_generation", False)
        if type(count) is not int or not 0 <= count <= 16 or type(hold) is not bool:
            raise ValueError("invalid_fixture_start")
        self.receiver.invalidate()
        self.core.prepare(count, hold)
        response_id = self.session.submit_text("架空の合成入力")
        binding = self.receiver.bind(response_id)
        await self.core.ready.wait()
        if not hold:
            await self.session.drain()
        segments = []
        position = 0
        while (packet := self.session.playback.consume()) is not None:
            with wave.open(io.BytesIO(packet.wav), "rb") as wav:
                rate, samples = wav.getframerate(), wav.getnframes()
            segment = dict(response_id=response_id, sequence=packet.sequence,
                           track_sid="synthetic-output", sample_rate=rate,
                           sample_start=position, sample_end=position + samples)
            if not self.receiver.record_segment(**segment):
                raise RuntimeError("fixture_segment_rejected")
            segments.append(segment)
            position += samples
        return dict(response_id=response_id, binding=binding, segments=segments,
                    generated=self.session.generated == response_id)

    async def dispatch(self, command: dict[str, Any]) -> dict[str, object]:
        name = command.get("command")
        if name == "start":
            return await self.start(command)
        if name == "receive":
            wire = command.get("wire")
            if not isinstance(wire, str):
                raise ValueError("invalid_fixture_wire")
            accepted = self.receiver.receive(
                wire.encode("utf-8"),
                participant_identity=command.get("participant_identity", "browser"),
                participant_sid=command.get("participant_sid", "participant"),
            )
            return {"accepted": accepted, "status": self.status()}
        if name == "finish_generation":
            self.core.release.set()
            await self.session.drain()
        elif name in {"cancel", "reconnect"}:
            # 分散側の停止通知より先に、host 所有の受付・Session を失効させる。
            self.receiver.invalidate()
            if name == "cancel":
                self.session.cancel()
            else:
                self.session.reconnect()
        elif name == "close":
            await self.close()
        elif name != "status":
            raise ValueError("unknown_fixture_command")
        return self.status()

    async def close(self) -> None:
        self.receiver.close()
        await self.session.close()


async def main() -> None:
    host = Host()
    try:
        while raw := await asyncio.to_thread(sys.stdin.buffer.readline, 65537):
            identifier: object = None
            closing = False
            try:
                if len(raw) > 65536:
                    raise ValueError("fixture_line_capacity_exceeded")
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError("invalid_fixture_command")
                identifier = command.get("id")
                async with asyncio.timeout(5):
                    result = await host.dispatch(command)
                response = {"id": identifier, "ok": True, "result": result}
                closing = command.get("command") == "close"
            except Exception:
                response = {"id": identifier, "ok": False, "error": "fixture_command_failed"}
            print(json.dumps(response, ensure_ascii=True), flush=True)
            if closing:
                break
    finally:
        await host.close()


if __name__ == "__main__":
    asyncio.run(main())
