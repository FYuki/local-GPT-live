"""host RPC 受信境界を実 Session と合成音声で検証する。実端末受入ではない。"""

import asyncio
import json

import pytest

from local_gpt_live.demo import FixtureStt, FixtureTts
from local_gpt_live.playback import Playback
from local_gpt_live.playback_ack_transport import MAX_SAFE_INTEGER, PlaybackAckTransport
from local_gpt_live.session import VoiceSession


class Core:
    async def stream(self, text):
        yield "最初の合成区間。次の合成区間。"


@pytest.fixture
async def harness():
    events = []
    session = VoiceSession(FixtureStt(), Core(), FixtureTts(), Playback(), emit=events.append)
    response_id = session.submit_text("合成入力")
    await session.drain()
    bridge = PlaybackAckTransport(session, "browser", "participant")
    binding = bridge.bind(response_id)
    packets = []
    while (packet := session.playback.consume()) is not None:
        packets.append(packet)
    assert len(packets) == 2
    h = {"session": session, "bridge": bridge, "response_id": response_id,
         "binding": binding, "packets": packets, "events": events}
    yield h
    bridge.close()
    await session.close()


def record(h, sequence=0, **changes):
    metadata = dict(response_id=h["response_id"], sequence=sequence, track_sid="output",
                    sample_rate=24000, sample_start=sequence * 480, sample_end=(sequence + 1) * 480)
    metadata.update(changes)
    return h["bridge"].record_segment(**metadata)


def wire(h, sequence=0, complete=False, **changes):
    message = dict(v=1, type="playback_complete" if complete else "playback_ack",
                   binding=h["binding"], response_id=h["response_id"])
    message["final_audio_sequence" if complete else "audio_sequence"] = sequence
    message.update(changes)
    return json.dumps(message, ensure_ascii=True).encode("utf-8")


def receive(h, payload, **changes):
    identity = dict(participant_identity="browser", participant_sid="participant")
    identity.update(changes)
    return h["bridge"].receive(payload, **identity)


async def test_metadata_and_ack_do_not_implicitly_complete(harness):
    h = harness
    assert record(h, 0) and record(h, 1)
    assert h["session"].playback.confirmed_sequence == -1
    assert not receive(h, wire(h, 1, complete=True))
    assert not receive(h, wire(h, 1))
    assert receive(h, wire(h, 0))
    assert receive(h, wire(h, 0))
    assert receive(h, wire(h, 1))
    assert h["session"].active == h["response_id"]
    assert not any(e.kind == "playback_completed" for e in h["events"])
    assert not receive(h, wire(h, 0, complete=True))
    assert receive(h, wire(h, 1, complete=True))
    assert receive(h, wire(h, 1, complete=True))
    assert not receive(h, wire(h, 1))
    assert sum(e.kind == "playback_completed" for e in h["events"]) == 1


async def test_unregistered_delivery_is_not_acknowledged(harness):
    assert not receive(harness, wire(harness))
    assert record(harness)
    assert receive(harness, wire(harness))
    assert not receive(harness, wire(harness, 1))


@pytest.mark.parametrize("changes", [
    {"participant_identity": "other"}, {"participant_sid": "rejoined"},
    {"participant_identity": None}, {"participant_sid": ""},
])
async def test_authenticated_participant_identity_and_sid_are_both_pinned(harness, changes):
    assert record(harness)
    assert not receive(harness, wire(harness), **changes)
    assert harness["session"].playback.confirmed_sequence == -1


@pytest.mark.parametrize("changes", [
    {"generation": 1}, {"extra": None}, {"v": True}, {"v": 1.0}, {"v": 2},
    {"binding": "other"}, {"binding": "x" * 257}, {"binding": None},
    {"response_id": "other"}, {"response_id": ""}, {"response_id": "\ud800"},
    {"response_id": "x\u0000"}, {"response_id": []}, {"audio_sequence": True},
    {"audio_sequence": 0.0}, {"audio_sequence": "0"}, {"audio_sequence": None},
    {"audio_sequence": -1}, {"audio_sequence": MAX_SAFE_INTEGER + 1},
    {"audio_sequence": MAX_SAFE_INTEGER}, {"audio_sequence": float("nan")},
    {"audio_sequence": float("inf")}, {"type": "unknown"},
])
async def test_strict_wire_fields_reject_without_state_change(harness, changes):
    assert record(harness)
    assert not receive(harness, wire(harness, **changes))
    assert harness["session"].playback.confirmed_sequence == -1


@pytest.mark.parametrize("payload", [
    b"", b"[1]", b"null", b"false", b"{}", b"{", b"\xff", b" " * 2049,
    b'{"type":"playback_ack","type":"playback_complete"}',
    b"[" * 1024 + b"]" * 1024,
])
async def test_malformed_or_oversized_json_is_rejected(harness, payload):
    assert not receive(harness, payload)


async def test_duplicate_field_even_with_identical_value_is_rejected(harness):
    assert record(harness)
    payload = wire(harness).replace(b'"v": 1,', b'"v": 1, "v": 1,')
    assert not receive(harness, payload)
    assert harness["session"].playback.confirmed_sequence == -1


@pytest.mark.parametrize("changes", [
    {"track_sid": "other"}, {"track_sid": ""}, {"sample_rate": 48000},
    {"sample_rate": 8000}, {"sample_rate": True}, {"sequence": 2},
    {"sequence": True}, {"sequence": MAX_SAFE_INTEGER + 1}, {"sample_start": 481},
    {"sample_end": 480}, {"sample_end": 960.0}, {"sample_end": MAX_SAFE_INTEGER + 1},
])
async def test_segment_metadata_is_contiguous_bounded_and_format_stable(harness, changes):
    assert record(harness)
    assert record(harness)  # 最新範囲の同一再通知だけを冪等に受ける。
    metadata = dict(sequence=1)
    metadata.update(changes)
    assert not record(harness, **metadata)
    assert not receive(harness, wire(harness, 1))


async def test_new_binding_for_same_response_revokes_old_wire_and_metadata(harness):
    h = harness
    assert record(h)
    old_wire = wire(h)
    old_binding = h["binding"]
    h["binding"] = h["bridge"].bind(h["response_id"])
    assert h["binding"] != old_binding
    assert not receive(h, old_wire)
    assert not receive(h, wire(h))
    assert record(h)
    assert receive(h, wire(h))


@pytest.mark.parametrize("operation", ["cancel", "reconnect", "new_response", "close", "invalidate"])
async def test_terminal_or_new_response_rejects_old_ack(harness, operation):
    h = harness
    assert record(h)
    if operation == "invalidate":
        h["bridge"].invalidate()
        assert h["session"].active == h["response_id"]
    elif operation == "new_response":
        h["session"].submit_text("次の合成入力")
        await h["session"].drain()
    elif operation == "close":
        h["bridge"].close()
    else:
        getattr(h["session"], operation)()
    assert not receive(h, wire(h))
    assert not receive(h, wire(h, 1, complete=True))


async def test_completion_receipt_is_revoked_after_invalidate_or_new_response(harness):
    h = harness
    for sequence in range(2):
        assert record(h, sequence)
        assert receive(h, wire(h, sequence))
    completed = wire(h, 1, complete=True)
    assert receive(h, completed)
    h["session"].submit_text("次の合成入力")
    await h["session"].drain()
    assert not receive(h, completed)
    h["bridge"].invalidate()
    assert not receive(h, completed)


async def test_zero_segment_response_requires_generated_and_explicit_complete():
    class EmptyCore:
        async def stream(self, text):
            if False:
                yield ""

    session = VoiceSession(FixtureStt(), EmptyCore(), FixtureTts(), Playback())
    bridge = PlaybackAckTransport(session, "browser", "participant")
    try:
        response_id = session.submit_text("合成入力")
        h = dict(session=session, bridge=bridge, response_id=response_id,
                 binding=bridge.bind(response_id))
        assert not receive(h, wire(h, -1, complete=True))
        assert not receive(h, wire(h, -1))
        await session.drain()
        assert session.active == response_id
        assert receive(h, wire(h, -1, complete=True))
        assert receive(h, wire(h, -1, complete=True))
        bridge.close()
        assert not receive(h, wire(h, -1, complete=True))
    finally:
        await session.close()


@pytest.mark.parametrize("terminal", ["generated", "timeout", "failure"])
async def test_ack_during_generation_cannot_imply_completion(terminal):
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedCore:
        async def stream(self, text):
            yield "最初の合成区間。"
            entered.set()
            await release.wait()
            if terminal == "failure":
                raise RuntimeError("合成失敗")

    session = VoiceSession(FixtureStt(), PausedCore(), FixtureTts(), Playback())
    bridge = PlaybackAckTransport(session, "browser", "participant")
    try:
        response_id = session.submit_text("合成入力")
        await entered.wait()
        h = dict(session=session, bridge=bridge, response_id=response_id,
                 binding=bridge.bind(response_id))
        assert session.playback.consume() is not None
        assert record(h)
        assert receive(h, wire(h))
        assert not receive(h, wire(h, complete=True))
        if terminal == "timeout":
            session._expire(response_id)
        release.set()
        await session.drain()
        assert receive(h, wire(h, complete=True)) is (terminal == "generated")
        if terminal != "generated":
            assert not receive(h, wire(h))
    finally:
        release.set()
        bridge.close()
        await session.close()


async def test_wire_cannot_ack_core_undelivered_packet():
    session = VoiceSession(FixtureStt(), Core(), FixtureTts(), Playback())
    bridge = PlaybackAckTransport(session, "browser", "participant")
    try:
        response_id = session.submit_text("合成入力")
        await session.drain()
        h = dict(session=session, bridge=bridge, response_id=response_id,
                 binding=bridge.bind(response_id))
        assert record(h)
        assert not receive(h, wire(h))
        assert session.playback.confirmed_sequence == -1
    finally:
        bridge.close()
        await session.close()
