"""副作用を持たない制御schemaと公開reasonの単体試験。"""

import asyncio
import json

import pytest

from local_gpt_live.host_rpc import (
    InvalidInputAck,
    RpcRejected,
    estimate_fields,
    identifier,
    parse_control,
    rejected_result,
)


def wire(kind="open_input", **fields):
    return json.dumps(dict(v=1, type=kind, session_id="session", binding="binding",
                           request_id="request", **fields))


def test_control_parses_track_and_host_revision_expectation():
    command = parse_control(wire(track_sid="track", expected_revision=0))
    assert (command.kind, command.track_sid, command.expected_revision) == ("open_input", "track", 0)


@pytest.mark.parametrize("payload", [
    "[]", "null", '{"v":1,"v":1}', "```json\n{}\n```",
    wire(track_sid="track", expected_revision=True),
    wire(track_sid="track", expected_revision=float("nan")),
    wire(track_sid="track", expected_revision=0, identity="self-reported"),
    wire("text", text="x" * 8192),
    wire("focus", enabled=1),
])
def test_invalid_schema_is_rejected(payload):
    with pytest.raises(RpcRejected):
        parse_control(payload)


@pytest.mark.parametrize("value", ["", "line\n", "\ud800", "x" * 257, True])
def test_identifier_rejects_values_unsafe_for_wire(value):
    assert not identifier(value)


def test_grant_integer_types_are_validated_without_constructing_a_grant():
    grant = dict(track_sid="track", request_id="request", input_generation=1, input_revision=True)
    with pytest.raises(InvalidInputAck) as error:
        parse_control(wire("input_ack", grant=grant))
    assert error.value.binding == "binding"
    assert rejected_result(error.value) == {"ok": False, "reason": "invalid_control"}


@pytest.mark.parametrize("error,reason", [
    (TimeoutError("synthetic-native-secret"), "input_timeout"),
    (asyncio.CancelledError(), "operation_invalidated"),
    (RuntimeError("synthetic-native-secret"), "operation_failed"),
    (RpcRejected("stale_binding"), "stale_binding"),
])
def test_boundary_uses_fixed_reason_without_native_details(error, reason):
    assert rejected_result(error) == {"ok": False, "reason": reason}


def test_estimate_serialization_preserves_ranges_and_unconfirmed_playback():
    from local_gpt_live.sent_audio import SentAudioProgress

    ledger = SentAudioProgress(downlink_delay_ns=0, sdk_queue_allowance_ns=0)
    scope = ledger.begin(response_id="response", generation=1, track_sid="track", sample_rate=16000)
    ledger.begin_block(scope=scope, audio_sequence=0, sample_start=0, sample_end=160)
    ledger.record(scope=scope, audio_sequence=0, sample_start=0, sample_end=160, completed_at_ns=1)
    ledger.freeze(1)
    fields = estimate_fields(ledger.snapshot(100_000_000))
    assert fields["submitted_sample_end"] == 160
    assert fields["estimated_sample_end"] == 0
    assert fields["frozen_at_ns"] == 1
    assert fields["basis"] == "sdk_submitted_elapsed"
    assert fields["real_playback_confirmed"] is False
