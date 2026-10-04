"""配信だけで再生完了を確定しない境界。実ブラウザの検証ではない。"""

import asyncio

import pytest

from local_gpt_live.demo import FixtureCore, FixtureStt, FixtureTts
from local_gpt_live.playback import AudioPacket, Playback
from local_gpt_live.session import VoiceSession


def delivered_playback():
    playback = Playback()
    playback.start("fixture-response")
    for sequence in range(3):
        playback.enqueue(AudioPacket("fixture-response", sequence, b"synthetic"))
    return playback


@pytest.mark.parametrize("sequence", [0, 1, 2, -1, True, 0.0, "0", None])
def test_ack_rejects_undelivered_and_invalid_indices(sequence):
    playback = delivered_playback()
    assert not playback.acknowledge("fixture-response", sequence)
    assert playback.confirmed_sequence == -1
    assert not playback.all_confirmed


def test_ack_rejects_gaps_and_is_idempotent():
    playback = delivered_playback()
    while playback.consume() is not None:
        pass
    assert not playback.all_confirmed
    assert not playback.acknowledge("fixture-response", 2)
    assert playback.acknowledge("fixture-response", 0)
    assert playback.acknowledge("fixture-response", 0)
    assert playback.confirmed_sequence == 0
    assert not playback.acknowledge("fixture-response", 2)
    assert playback.acknowledge("fixture-response", 1)
    assert playback.acknowledge("fixture-response", 2)
    assert playback.all_confirmed
    assert not playback.acknowledge("fixture-response", 3)


def test_new_response_and_stop_revoke_old_ack():
    playback = delivered_playback()
    playback.consume()
    assert playback.acknowledge("fixture-response", 0)
    playback.start("next-response")
    assert playback.confirmed_sequence == -1
    assert not playback.acknowledge("fixture-response", 0)
    playback.enqueue(AudioPacket("next-response", 0, b"synthetic"))
    playback.consume()
    playback.stop()
    assert not playback.acknowledge("next-response", 0)
    assert not playback.all_confirmed


async def test_generation_and_delivery_do_not_prove_playback_completion():
    events = []
    session = VoiceSession(FixtureStt(), FixtureCore(), FixtureTts(), Playback(),
                           emit=events.append)
    try:
        response_id = session.submit_text("合成入力")
        await session.drain()
        assert session.generated == response_id
        assert not session.playback_completed(response_id)
        packets = []
        while (packet := session.playback.consume()) is not None:
            packets.append(packet)
        assert packets
        assert session.playback.pending_bytes == 0
        assert not session.playback_completed(response_id)
        assert not any(event.kind == "playback_completed" for event in events)
        for packet in packets:
            assert session.acknowledge_playback(packet.response_id, packet.sequence)
        assert session.playback_completed(response_id)
        assert not session.playback_completed(response_id)
        assert not session.acknowledge_playback(response_id, packets[-1].sequence)
        assert sum(event.kind == "playback_completed" for event in events) == 1
    finally:
        await session.close()


async def test_ack_can_arrive_before_generation_finishes():
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedCore:
        async def stream(self, text):
            yield "最初の合成区間。"
            entered.set()
            await release.wait()
            yield "次の合成区間。"

    session = VoiceSession(FixtureStt(), PausedCore(), FixtureTts(), Playback())
    try:
        response_id = session.submit_text("合成入力")
        await entered.wait()
        packet = session.playback.consume()
        assert session.acknowledge_playback(response_id, packet.sequence)
        assert not session.playback_completed(response_id)
        release.set()
        await session.drain()
        assert not session.playback_completed(response_id)
        packet = session.playback.consume()
        assert session.acknowledge_playback(response_id, packet.sequence)
        assert session.playback_completed(response_id)
    finally:
        release.set()
        await session.close()


@pytest.mark.parametrize("operation", ["cancel", "reconnect", "supersede", "close"])
async def test_session_revokes_ack_on_terminal_or_new_response(operation):
    session = VoiceSession(FixtureStt(), FixtureCore(), FixtureTts(), Playback())
    try:
        old = session.submit_text("合成入力")
        await session.drain()
        packet = session.playback.consume()
        if operation == "supersede":
            session.submit_text("次の合成入力")
            await session.drain()
        elif operation == "close":
            await session.close()
        else:
            getattr(session, operation)()
        assert not session.acknowledge_playback(old, packet.sequence)
        assert not session.playback_completed(old)
    finally:
        await session.close()
