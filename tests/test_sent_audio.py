"""送出時間推定を決定時刻で検証する。Roomや実音声出力は使用しない。"""

from dataclasses import FrozenInstanceError, replace

import pytest

from local_gpt_live.sent_audio import SentAudioProgress

SECOND = 1_000_000_000
MS = 1_000_000


def ledger(**config):
    progress = SentAudioProgress(**config)
    scope = progress.begin(response_id="response", generation=4, track_sid="track",
                           sample_rate=16000)
    return progress, scope


def block(progress, scope, sequence=0, start=0, end=320):
    return progress.begin_block(scope=scope, audio_sequence=sequence,
                                sample_start=start, sample_end=end)


def record(progress, scope, start=0, end=160, at=SECOND, sequence=0):
    return progress.record(scope=scope, audio_sequence=sequence, sample_start=start,
                           sample_end=end, completed_at_ns=at)


def test_default_allowances_and_discrete_frame_prefix():
    progress, scope = ledger()
    block(progress, scope)
    record(progress, scope)
    record(progress, scope, start=160, end=320, at=SECOND)
    before = progress.snapshot(SECOND + 409 * MS)
    assert (before.submitted_sample_end, before.estimated_sample_end) == (320, 0)
    partial = progress.snapshot(SECOND + 410 * MS)
    assert partial.estimated_sample_end == 160
    assert partial.estimated_audio_sequence == -1
    assert not partial.estimated_complete
    assert partial.blocks[0].estimated_sample_end == 160
    final = progress.snapshot(SECOND + 420 * MS)
    assert final.estimated_sample_end == 320
    assert final.estimated_audio_sequence == 0
    assert final.estimated_complete
    assert final.basis == "sdk_submitted_elapsed"
    assert final.real_playback_confirmed is False


def test_instant_capture_does_not_skip_audio_duration_and_supply_gap_is_not_credit():
    progress, scope = ledger(downlink_delay_ns=0, sdk_queue_allowance_ns=0)
    block(progress, scope, end=480)
    record(progress, scope)
    record(progress, scope, start=160, end=320)
    record(progress, scope, start=320, end=480, at=10 * SECOND)
    assert progress.snapshot(SECOND).estimated_sample_end == 0
    assert progress.snapshot(SECOND + 19 * MS).estimated_sample_end == 160
    assert progress.snapshot(SECOND + 20 * MS).estimated_sample_end == 320
    assert progress.snapshot(10 * SECOND).estimated_sample_end == 320
    assert progress.snapshot(10 * SECOND + 9 * MS).estimated_sample_end == 320
    assert progress.snapshot(10 * SECOND + 10 * MS).estimated_sample_end == 480


def test_historical_queries_filter_records_but_include_registered_plans():
    progress, scope = ledger(downlink_delay_ns=0, sdk_queue_allowance_ns=0)
    block(progress, scope)
    block(progress, scope, sequence=1, start=320, end=640)
    record(progress, scope)
    record(progress, scope, start=160, end=320, at=2 * SECOND)
    future = progress.snapshot(100 * SECOND)
    assert future.estimated_audio_sequence == 0
    assert not future.estimated_complete
    earlier = progress.snapshot(SECOND)
    assert earlier.submitted_sample_end == 160
    assert earlier.estimated_sample_end == 0
    assert len(earlier.blocks) == 2
    first, second = earlier.blocks
    assert (first.sample_start, first.sample_end, first.submitted_sample_end) == (0, 320, 160)
    assert first.first_submitted_at_ns == first.last_submitted_at_ns == SECOND
    assert second.submitted_sample_end == second.estimated_sample_end == second.sample_start
    assert second.first_submitted_at_ns is second.last_submitted_at_ns is None
    assert progress.snapshot(SECOND - 1).submitted_sample_end == 0
    assert progress.snapshot(100 * SECOND) == future


def test_blocks_report_partial_submitted_and_complete_estimated_prefix_separately():
    progress, scope = ledger(downlink_delay_ns=0, sdk_queue_allowance_ns=0)
    block(progress, scope, end=160)
    block(progress, scope, sequence=1, start=160, end=480)
    record(progress, scope)
    record(progress, scope, start=160, end=320, sequence=1, at=2 * SECOND)
    snapshot = progress.snapshot(100 * SECOND)
    assert snapshot.estimated_audio_sequence == 0
    assert snapshot.submitted_sample_end == snapshot.estimated_sample_end == 320
    assert snapshot.blocks[1].sample_end == 480
    assert snapshot.blocks[1].submitted_sample_end == 320
    assert snapshot.blocks[1].estimated_sample_end == 320
    assert not snapshot.estimated_complete


def test_freeze_is_idempotent_stops_estimate_and_refuses_late_mutations():
    progress, scope = ledger(downlink_delay_ns=0, sdk_queue_allowance_ns=0)
    block(progress, scope)
    record(progress, scope)
    record(progress, scope, start=160, end=320)
    frozen = progress.freeze(SECOND + 10 * MS)
    assert frozen.estimated_sample_end == 160
    assert frozen.frozen_at_ns == SECOND + 10 * MS
    assert progress.freeze(100 * SECOND) is frozen
    later = progress.snapshot(100 * SECOND)
    assert later.effective_at_ns == frozen.effective_at_ns
    assert later.estimated_sample_end == frozen.estimated_sample_end
    assert not record(progress, scope, start=160, end=320)
    assert not block(progress, scope, sequence=1, start=320, end=480)
    assert progress.snapshot(SECOND).estimated_sample_end == 0


def test_historical_freeze_excludes_later_records_without_clock_exception():
    progress, scope = ledger()
    block(progress, scope)
    record(progress, scope)
    record(progress, scope, start=160, end=320, at=2 * SECOND)
    frozen = progress.freeze(SECOND)
    assert frozen.submitted_sample_end == 160
    assert progress.snapshot(100 * SECOND).submitted_sample_end == 160
    assert progress.snapshot(100 * SECOND).estimated_sample_end == 0


def test_new_scope_rejects_old_generation_and_copied_or_rate_changed_scopes():
    progress, old = ledger()
    block(progress, old)
    record(progress, old)
    current = progress.begin(response_id="next", generation=5, track_sid="next-track",
                             sample_rate=48000)
    assert not record(progress, old)
    assert not block(progress, old)
    assert not block(progress, replace(current))
    assert not block(progress, replace(current, sample_rate=16000))
    assert block(progress, current, end=480)
    assert record(progress, current, end=480)
    snapshot = progress.snapshot(2 * SECOND)
    assert (snapshot.response_id, snapshot.generation, snapshot.track_sid,
            snapshot.sample_rate) == ("next", 5, "next-track", 48000)
    assert snapshot.submitted_sample_end == snapshot.estimated_sample_end == 480
    recreated = progress.begin(response_id="next", generation=5, track_sid="next-track",
                               sample_rate=48000)
    assert not block(progress, current)
    assert recreated is not current


def test_duplicate_notifications_do_not_change_original_submission_time_or_capacity():
    progress, scope = ledger(max_records=1, max_blocks=1)
    assert block(progress, scope, end=160)
    assert record(progress, scope)
    original = progress.snapshot(2 * SECOND)
    assert block(progress, scope, end=160)
    assert record(progress, scope, at=3 * SECOND)
    assert progress.snapshot(2 * SECOND) == original
    assert progress.snapshot(4 * SECOND).blocks[0].last_submitted_at_ns == SECOND


def test_old_record_redelivery_after_new_record_is_idempotent():
    progress, scope = ledger()
    block(progress, scope)
    record(progress, scope)
    record(progress, scope, start=160, end=320, at=2 * SECOND)
    assert record(progress, scope)
    assert progress.snapshot(3 * SECOND).blocks[0].last_submitted_at_ns == 2 * SECOND


@pytest.mark.parametrize("sequence,start,end", [(1, 0, 160), (0, 1, 160), (0, 0, 0),
                                                (True, 0, 160), (0, False, 160)])
def test_block_plan_requires_sequence_zero_and_contiguous_positive_samples(sequence, start, end):
    progress, scope = ledger()
    with pytest.raises(ValueError):
        block(progress, scope, sequence, start, end)
    assert progress.snapshot(SECOND).blocks == ()


def test_changed_duplicate_or_noncontiguous_block_plan_does_not_mutate_ledger():
    progress, scope = ledger()
    block(progress, scope)
    before = progress.snapshot(SECOND)
    for sequence, start, end in [(0, 0, 160), (2, 320, 480), (1, 321, 480), (1, 160, 480)]:
        with pytest.raises(ValueError):
            block(progress, scope, sequence, start, end)
    assert progress.snapshot(SECOND) == before


@pytest.mark.parametrize("start,end,sequence", [(1, 160, 0), (0, 321, 0), (0, 160, 1),
                                               (0, 0, 0), (False, 160, 0)])
def test_unplanned_hole_overlap_and_invalid_record_ranges_are_rejected(start, end, sequence):
    progress, scope = ledger()
    block(progress, scope)
    with pytest.raises(ValueError):
        record(progress, scope, start, end, sequence=sequence)
    assert progress.snapshot(2 * SECOND).submitted_sample_end == 0
    record(progress, scope)
    with pytest.raises(ValueError):
        record(progress, scope, start=80, end=240)
    assert progress.snapshot(2 * SECOND).submitted_sample_end == 160


def test_new_record_clock_regression_is_rejected_and_can_recover():
    progress, scope = ledger()
    block(progress, scope)
    record(progress, scope)
    with pytest.raises(ValueError, match="clock_regressed"):
        record(progress, scope, start=160, end=320, at=SECOND - 1)
    assert progress.snapshot(2 * SECOND).submitted_sample_end == 160
    assert record(progress, scope, start=160, end=320, at=SECOND)


def test_capacity_errors_preserve_existing_records_and_plans():
    progress, scope = ledger(max_records=1, max_blocks=1)
    block(progress, scope)
    record(progress, scope)
    before = progress.snapshot(3 * SECOND)
    with pytest.raises(ValueError, match="record_capacity"):
        record(progress, scope, start=160, end=320)
    with pytest.raises(ValueError, match="block_capacity"):
        block(progress, scope, sequence=1, start=320, end=480)
    assert progress.snapshot(3 * SECOND) == before
    assert not before.estimated_complete


def test_nan_float_boolean_and_negative_clock_values_are_rejected():
    progress, scope = ledger()
    block(progress, scope)
    for at in [True, -1, 1.0, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            record(progress, scope, at=at)
        with pytest.raises(ValueError):
            progress.snapshot(at)
    assert progress.snapshot(SECOND).submitted_sample_end == 0


@pytest.mark.parametrize("config", [dict(downlink_delay_ns=-1), dict(sdk_queue_allowance_ns=True),
                                   dict(max_records=0), dict(max_blocks=1.5)])
def test_invalid_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        SentAudioProgress(**config)


@pytest.mark.parametrize("changes", [dict(response_id=""), dict(track_sid="\n"),
                                    dict(generation=True), dict(sample_rate=48000.0),
                                    dict(sample_rate=8000)])
def test_invalid_scope_is_rejected_without_resetting_existing_ledger(changes):
    progress, scope = ledger()
    block(progress, scope)
    record(progress, scope)
    before = progress.snapshot(2 * SECOND)
    args = dict(response_id="response", generation=4, track_sid="track", sample_rate=16000)
    with pytest.raises(ValueError):
        progress.begin(**(args | changes))
    assert progress.snapshot(2 * SECOND) == before


def test_empty_ledger_is_not_response_completion_and_snapshot_is_immutable():
    progress = SentAudioProgress()
    with pytest.raises(ValueError, match="scope_unavailable"):
        progress.snapshot(0)
    progress, _ = ledger()
    snapshot = progress.snapshot(0)
    assert not snapshot.estimated_complete
    assert snapshot.estimated_audio_sequence == -1
    assert snapshot.estimated_sample_end == snapshot.submitted_sample_end == 0
    with pytest.raises(FrozenInstanceError):
        snapshot.estimated_complete = True


def test_fractional_nanosecond_duration_rounds_up_without_early_prefix():
    progress = SentAudioProgress(downlink_delay_ns=0, sdk_queue_allowance_ns=0)
    scope = progress.begin(response_id="response", generation=0, track_sid="track",
                           sample_rate=44100)
    block(progress, scope, end=1)
    record(progress, scope, end=1, at=0)
    duration_ns = (SECOND + 44100 - 1) // 44100
    assert progress.snapshot(duration_ns - 1).estimated_sample_end == 0
    assert progress.snapshot(duration_ns).estimated_sample_end == 1
