import asyncio
import json
from pathlib import Path

import pytest

from local_gpt_live.demo import FixtureCore, FixtureStt, FixtureTts, run_scenario
from local_gpt_live.playback import AudioPacket, Playback
from local_gpt_live.session import VoiceSession
from local_gpt_live.turn_decision import classify_turn


@pytest.mark.parametrize("text,expected", [
    ("うん。", "backchannel"), ("ｳﾝｳﾝ", "backchannel"),
    ("そうですね！", "backchannel"), ("ヘッ", "indeterminate"),
    ("", "indeterminate"), ("ん？", "take_turn"),
    ("はい、続きを止めて", "take_turn"), ("そうですね、でも質問があります", "take_turn"),
])
def test_poc_turn_decisions(text, expected):
    assert classify_turn(text) == expected


@pytest.mark.parametrize("scenario", json.loads(
    (Path(__file__).parents[1] / "src/local_gpt_live/fixtures/scenarios.json").read_text()))
async def test_fixture_acceptance(scenario):
    events = await run_scenario(scenario["events"])
    kinds = [event.kind for event in events]
    assert set(scenario["expect"]) <= set(kinds)
    assert not set(scenario.get("forbid", [])) & set(kinds)


async def test_backchannel_keeps_generated_but_unplayed_output():
    stt, events, playback = FixtureStt(), [], Playback()
    session = VoiceSession(stt, FixtureCore(), FixtureTts(), playback, emit=events.append)
    try:
        old = session.submit_text("説明")
        await session.drain()
        old_bytes = playback.pending_bytes
        pcm = b"\x01\x04" * 1600
        stt.transcripts[pcm] = "うん"
        session.submit_audio(pcm, generation=session.generation, overlap=old)
        await session.drain()
        assert session.active == old and playback.pending_bytes == old_bytes
        stt.transcripts[pcm] = "別の質問"
        session.submit_audio(pcm, generation=session.generation, overlap=old)
        await session.drain()
        new = session.active
        assert new != old
        assert playback.consume().response_id == new
        kinds = [e.kind for e in events]
        assert kinds.index("playback_stopped") < kinds.index("response_cancelled")
    finally:
        await session.close()


async def test_late_tts_ignoring_cancel_cannot_enqueue_after_new_response():
    entered, release = asyncio.Event(), asyncio.Event()

    class LateTts(FixtureTts):
        calls = 0

        async def synthesize(self, text):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            return await super().synthesize(text)

    playback, events = Playback(), []
    session = VoiceSession(FixtureStt(), FixtureCore(), LateTts(), playback, emit=events.append)
    try:
        old = session.submit_text("前")
        await entered.wait()
        new = session.submit_text("後")
        release.set()
        await session.drain()
        assert playback.consume().response_id == new
        assert playback.consume() is None
        assert not any(e.kind == "audio_queued" and e.response_id == old for e in events)
    finally:
        release.set()
        await session.close()


async def test_reconnect_discards_late_stt_and_old_generation():
    entered, release = asyncio.Event(), asyncio.Event()

    class LateStt:
        async def transcribe(self, pcm):
            entered.set()
            await release.wait()
            return "古い合成入力"

    session = VoiceSession(LateStt(), FixtureCore(), FixtureTts(), Playback())
    try:
        generation = session.generation
        session.submit_audio(b"\x01\x04" * 1600, generation=generation, overlap=None)
        await entered.wait()
        session.reconnect()
        assert not session.submit_audio(b"\x01\x04", generation=generation, overlap=None)
        release.set()
        await session.drain()
        assert session.active is None and session.playback.pending_bytes == 0
    finally:
        release.set()
        await session.close()


async def test_stt_queue_has_limit_and_preserves_order():
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    class Stt:
        async def transcribe(self, pcm):
            seen.append(pcm)
            entered.set()
            await release.wait()
            return "fixture"

    session = VoiceSession(Stt(), FixtureCore(), FixtureTts(), Playback())
    try:
        assert session.submit_audio(b"aa", generation=0, overlap=None)
        await entered.wait()
        for pcm in (b"bb", b"cc", b"dd"):
            assert session.submit_audio(pcm, generation=0, overlap=None)
        assert not session.submit_audio(b"ee", generation=0, overlap=None)
        release.set()
        await session.drain()
        assert seen == [b"aa", b"bb", b"cc", b"dd"]
    finally:
        release.set()
        await session.close()


async def test_stt_timeout_keeps_response_and_accepts_next_input():
    class Stt:
        calls = 0

        async def transcribe(self, pcm):
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            return "再開"

    events = []
    session = VoiceSession(Stt(), FixtureCore(), FixtureTts(), Playback(),
                           emit=events.append, stt_timeout=0.01)
    try:
        session.submit_audio(b"aa", generation=0, overlap=None)
        await session.drain()
        assert any(e.detail == "stt_timeout" for e in events)
        session.submit_audio(b"bb", generation=0, overlap=None)
        await session.drain()
        assert session.playback.pending_bytes > 0
    finally:
        await session.close()


def test_playback_rejects_stale_duplicate_and_capacity():
    playback = Playback(max_bytes=4)
    playback.start("new")
    assert not playback.enqueue(AudioPacket("old", 0, b"aa"))
    assert playback.enqueue(AudioPacket("new", 0, b"aa"))
    with pytest.raises(ValueError, match="sequence"):
        playback.enqueue(AudioPacket("new", 0, b"aa"))
    with pytest.raises(ValueError, match="capacity"):
        playback.enqueue(AudioPacket("new", 1, b"aaaa"))
    playback.stop()
    assert playback.consume() is None


async def test_tts_failure_clears_earlier_audio_and_next_turn_recovers():
    class Core:
        async def stream(self, text):
            yield "最初の区間。"
            yield "次の区間。"

    class Tts(FixtureTts):
        calls = 0

        async def synthesize(self, text):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("private provider payload")
            return await super().synthesize(text)

    events = []
    session = VoiceSession(FixtureStt(), Core(), Tts(), Playback(), emit=events.append)
    try:
        session.submit_text("前")
        await session.drain()
        assert session.active is None and session.playback.consume() is None
        assert any(e.kind == "response_failed" for e in events)
        assert "private provider payload" not in repr(events)
        new = session.submit_text("後")
        await session.drain()
        assert session.playback.consume().response_id == new
    finally:
        await session.close()
