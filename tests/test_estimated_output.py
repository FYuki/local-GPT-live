"""BE時間推定によるSession終端を、確定ACKと分離して合成検証する。"""

import asyncio
import io
import wave
from dataclasses import replace

import pytest

from local_gpt_live.demo import FixtureStt, FixtureTts
from local_gpt_live.playback import AudioPacket, Playback
from local_gpt_live.sent_audio import SentAudioProgress
from local_gpt_live.session import VoiceSession


class Core:
    def __init__(self, text="一。"):
        self.text = text

    async def stream(self, text):
        yield self.text


class FreezingPlayback(Playback):
    """SDKの代わりに所有台帳を同期固定する。ACK呼出しも独立に観測する。"""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.ack_calls = []

    def stop(self):
        response_id = self.active
        super().stop()
        if response_id in self.owner.ledgers:
            self.owner.ledgers[response_id].freeze(self.owner.now_ns)

    def acknowledge(self, response_id, sequence):
        self.ack_calls.append((response_id, sequence))
        return super().acknowledge(response_id, sequence)


class Harness:
    def __init__(self, *, attach=True):
        self.now_ns = 1_000_000_000
        self.ledgers = {}
        self.events = []
        self.transform = lambda snapshot: snapshot
        self.playback = FreezingPlayback(self)
        self.session = VoiceSession(FixtureStt(), Core(), FixtureTts(), self.playback,
                                    emit=self.events.append)
        if attach:
            self.session.set_output_estimator(self.estimate)

    def estimate(self, response_id):
        ledger = self.ledgers.get(response_id)
        snapshot = ledger.snapshot(self.now_ns) if ledger is not None else None
        return self.transform(snapshot)

    async def start(self, text="一。"):
        self.session.core = Core(text)
        response_id = self.session.submit_text("合成入力")
        await self.session.drain()
        return response_id

    def consume(self):
        packets = []
        while (packet := self.playback.consume()) is not None:
            packets.append(packet)
        return packets

    def register(self, response_id, packets, *, partial_samples=None):
        ledger = SentAudioProgress()
        scope = ledger.begin(response_id=response_id, generation=self.session.generation,
                              track_sid="TR-" + response_id, sample_rate=16000)
        self.ledgers[response_id] = ledger
        start = 0
        for packet in packets:
            with wave.open(io.BytesIO(packet.wav)) as wav:
                frames = wav.getnframes()
                assert wav.getframerate() == 16000
            ledger.begin_block(scope=scope, audio_sequence=packet.sequence,
                                sample_start=start, sample_end=start + frames)
            submitted = frames if partial_samples is None else partial_samples
            if submitted:
                ledger.record(scope=scope, audio_sequence=packet.sequence,
                               sample_start=start, sample_end=start + submitted,
                               completed_at_ns=self.now_ns)
            start += frames
        return ledger, scope


@pytest.fixture
async def harness():
    h = Harness()
    try:
        yield h
    finally:
        await h.session.close()


async def test_estimated_completion_freezes_output_without_ack_or_input_generation_change(harness):
    h = harness
    response_id = await h.start("一。二。")
    h.register(response_id, h.consume())
    generation = h.session.generation
    h.now_ns = 2_000_000_000
    assert h.estimate(response_id).estimated_complete
    assert not h.session.playback_completed(response_id)
    assert h.session.estimated_output_completed(response_id)

    assert h.session.active is h.playback.active is None
    assert h.session.generation == generation
    assert h.playback.ack_calls == []
    assert h.playback.confirmed_sequence == -1
    assert not h.playback.all_confirmed
    stopped = h.session.last_output_estimate
    assert stopped.response_id == response_id
    assert stopped.frozen_at_ns == h.now_ns
    assert stopped.estimated_audio_sequence == 1
    assert stopped.real_playback_confirmed is False
    assert [(e.kind, e.detail) for e in h.events if e.kind.startswith("output_estimated")] == [
        ("output_estimated_completed", "sdk_submitted_elapsed"),
    ]
    assert not any(e.kind == "playback_completed" for e in h.events)
    assert not h.session.estimated_output_completed(response_id)
    assert not h.session.acknowledge_playback(response_id, 0)


async def test_current_output_response_refreshes_only_after_estimated_deadline(harness):
    h = harness
    response_id = await h.start()
    ledger, _ = h.register(response_id, h.consume())
    deadline = ledger.next_estimated_complete_at_ns()
    h.now_ns = deadline - 1
    assert h.session.current_output_response() == response_id
    h.now_ns = deadline
    assert h.session.current_output_response() is None
    assert h.session.current_output_response() is None
    assert sum(e.kind == "output_estimated_completed" for e in h.events) == 1


async def test_complete_ledger_during_generation_cannot_end_response(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedCore:
        async def stream(self, text):
            yield "一。"
            entered.set()
            await release.wait()

    h.session.core = GatedCore()
    response_id = h.session.submit_text("合成入力")
    try:
        await entered.wait()
        h.register(response_id, h.consume())
        h.now_ns = 2_000_000_000
        assert h.estimate(response_id).estimated_complete
        assert h.session.generated is None
        assert not h.session.estimated_output_completed(response_id)
        assert h.session.current_output_response() == response_id
    finally:
        release.set()
    await h.session.drain()
    assert h.session.estimated_output_completed(response_id)


async def test_unconsumed_packet_keeps_output_open_even_if_estimator_claims_complete(harness):
    h = harness
    response_id = await h.start()
    template = await FixtureTts().synthesize("合成区間")
    h.register(response_id, [AudioPacket(response_id, 0, template)])
    h.now_ns = 2_000_000_000
    assert h.estimate(response_id).estimated_complete
    assert h.playback.pending_bytes > 0
    assert not h.session.estimated_output_completed(response_id)
    assert h.session.current_output_response() == response_id
    assert len(h.consume()) == 1
    assert h.session.estimated_output_completed(response_id)


@pytest.mark.parametrize("submitted", [0, 800])
async def test_partial_sdk_submission_never_finishes_from_elapsed_time(harness, submitted):
    h = harness
    response_id = await h.start()
    h.register(response_id, h.consume(), partial_samples=submitted)
    h.now_ns = 100_000_000_000
    assert h.estimate(response_id).submitted_sample_end == submitted
    assert not h.session.estimated_output_completed(response_id)
    assert h.session.active == response_id
    assert h.session.last_output_estimate is None


async def test_complete_first_block_cannot_hide_later_enqueued_unregistered_block(harness):
    h = harness
    response_id = await h.start("一。二。")
    first, second = h.consume()
    h.register(response_id, [first])
    h.now_ns = 2_000_000_000
    assert h.playback.pending_bytes == 0
    assert h.playback.last_audio_sequence == second.sequence == 1
    assert h.estimate(response_id).estimated_complete
    assert len(h.estimate(response_id).blocks) == 1
    assert not h.session.estimated_output_completed(response_id)
    assert h.session.current_output_response() == response_id


@pytest.mark.parametrize("fault", ["missing", "response", "generation", "frozen",
                                   "sequence", "incomplete", "partial", "block_sequence"])
async def test_estimator_must_describe_current_unfrozen_complete_response(harness, fault):
    h = harness
    response_id = await h.start()
    h.register(response_id, h.consume())
    h.now_ns = 2_000_000_000
    valid = h.estimate(response_id)
    if fault == "missing":
        invalid = None
    elif fault == "response":
        invalid = replace(valid, response_id="another-response")
    elif fault == "generation":
        invalid = replace(valid, generation=valid.generation + 1)
    elif fault == "frozen":
        invalid = replace(valid, frozen_at_ns=h.now_ns)
    elif fault == "sequence":
        invalid = replace(valid, estimated_audio_sequence=-1)
    elif fault == "incomplete":
        invalid = replace(valid, estimated_complete=False)
    elif fault == "partial":
        invalid = replace(valid, blocks=(replace(valid.blocks[0], submitted_sample_end=800),))
    else:
        invalid = replace(valid, blocks=(replace(valid.blocks[0], audio_sequence=1),))
    h.transform = lambda _: invalid
    assert not h.session.estimated_output_completed(response_id)
    assert h.session.current_output_response() == response_id
    assert h.playback.ack_calls == []
    h.transform = lambda snapshot: snapshot
    assert h.session.estimated_output_completed(response_id)


async def test_stopped_playback_or_wrong_callback_id_cannot_finish_session(harness):
    h = harness
    response_id = await h.start()
    h.register(response_id, h.consume())
    h.now_ns = 2_000_000_000
    assert not h.session.estimated_output_completed("wrong-response")
    h.playback.stop()
    assert h.session.active == response_id
    assert not h.session.estimated_output_completed(response_id)


async def test_old_completion_callback_cannot_finish_new_response(harness):
    h = harness
    old_id = await h.start()
    h.register(old_id, h.consume())
    h.now_ns = 2_000_000_000
    new_id = await h.start()
    h.register(new_id, h.consume())
    h.now_ns = 3_000_000_000
    assert not h.session.estimated_output_completed(old_id)
    assert h.session.active == new_id
    assert h.session.estimated_output_completed(new_id)
    assert [(e.response_id, e.kind) for e in h.events if e.kind == "output_estimated_completed"] == [
        (new_id, "output_estimated_completed"),
    ]


@pytest.mark.parametrize("operation,reason", [("cancel", "cancel"), ("take_turn", "take_turn"),
                                             ("superseded", "superseded"),
                                             ("close", "closed")])
async def test_stop_records_frozen_partial_estimate_after_generation_changes(harness,
                                                                          operation, reason):
    h = harness
    response_id = await h.start()
    h.register(response_id, h.consume(), partial_samples=800)
    original_generation = h.session.generation
    h.now_ns = 2_000_000_000
    if operation == "cancel":
        h.session.cancel()
    elif operation == "take_turn":
        pcm = b"\x20\x00" * 1600
        h.session.stt.transcripts[pcm] = "待って、質問です。"
        assert await h.session.preview(pcm, generation=h.session.generation,
                                       overlap=response_id, is_current=lambda: True)
    elif operation == "superseded":
        await h.start()
    else:
        await h.session.close()
    stopped = h.session.last_output_estimate
    assert stopped.response_id == response_id
    assert stopped.generation == original_generation
    assert stopped.frozen_at_ns == h.now_ns
    assert stopped.submitted_sample_end == stopped.estimated_sample_end == 800
    assert stopped.estimated_audio_sequence == -1
    assert not stopped.estimated_complete
    if operation != "take_turn":
        assert h.session.generation > original_generation
    assert [(e.response_id, e.detail) for e in h.events if e.kind == "output_estimated_stopped"] == [
        (response_id, reason),
    ]
    assert h.playback.ack_calls == []
    assert not h.session.estimated_output_completed(response_id)


async def test_last_output_estimate_replaces_prior_result_with_one_current_stop(harness):
    h = harness
    first = await h.start()
    h.register(first, h.consume())
    h.now_ns = 2_000_000_000
    h.session.cancel()
    prior = h.session.last_output_estimate
    second = await h.start()
    h.register(second, h.consume(), partial_samples=800)
    h.now_ns = 3_000_000_000
    h.session.cancel()
    assert h.session.last_output_estimate.response_id == second
    assert h.session.last_output_estimate is not prior
    assert prior.response_id == first


async def test_generated_empty_response_can_end_without_ledger_or_audio_ack(harness):
    h = harness
    response_id = await h.start("")
    assert h.session.generated == response_id
    assert h.playback.last_audio_sequence == -1
    assert h.session.last_output_estimate is None
    generation = h.session.generation
    assert h.session.current_output_response() is None
    assert h.session.generation == generation
    assert h.session.last_output_estimate is None
    assert h.playback.ack_calls == []
    assert [(e.kind, e.detail) for e in h.events if e.kind.startswith("output_estimated")] == [
        ("output_estimated_completed", "no_audio"),
    ]


@pytest.mark.parametrize("text", ["", "一。"])
async def test_unattached_base_playback_keeps_existing_explicit_ack_completion(text):
    h = Harness(attach=False)
    try:
        response_id = await h.start(text)
        packets = h.consume()
        h.now_ns = 100_000_000_000
        assert not h.session.estimated_output_completed(response_id)
        assert h.session.current_output_response() == response_id
        for packet in packets:
            assert h.session.acknowledge_playback(response_id, packet.sequence)
        assert h.session.playback_completed(response_id)
        assert not any(e.kind.startswith("output_estimated") for e in h.events)
    finally:
        await h.session.close()


async def test_output_estimator_attachment_is_single_owner_and_rejects_active_or_closed():
    h = Harness(attach=False)
    try:
        await h.start()
        with pytest.raises(ValueError, match="attach_before_response"):
            h.session.set_output_estimator(h.estimate)
        h.session.cancel()
        h.session.set_output_estimator(h.estimate)
        with pytest.raises(ValueError, match="attach_before_response"):
            h.session.set_output_estimator(h.estimate)
    finally:
        await h.session.close()
    closed = Harness(attach=False)
    await closed.session.close()
    with pytest.raises(ValueError, match="attach_before_response"):
        closed.session.set_output_estimator(closed.estimate)
