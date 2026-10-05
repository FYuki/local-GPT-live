"""数量や合成統計が元 PCM 区間の確定証拠に昇格しないことを検査する。"""

import importlib.util
import json
import sys
from pathlib import Path

from livekit import rtc
from livekit.rtc._proto.stats_pb2 import InboundRtpStreamStats

from local_gpt_live.livekit_transport import SegmentSent


spec = importlib.util.spec_from_file_location(
    "rtp_observation", Path(__file__).resolve().parents[1] / "tools/rtp_observation.py",
)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


def test_sdk_frame_userdata_is_not_transmitted_in_frame_buffer():
    frame = probe.fixture_frame()
    frame.userdata.update(source_sample_start=160, response_id="fixture-response")
    fields = {field.name for field in frame._proto_info().DESCRIPTOR.fields}
    assert fields == {"data_ptr", "num_channels", "sample_rate", "samples_per_channel"}
    assert "source_sample_start" in frame.userdata
    assert "source_sample_start" not in fields
    assert "rtp_timestamp" not in fields


def test_equal_counts_survive_pcm_substitution_without_proving_source_offset():
    segments = [SegmentSent("fixture", 0, "fixture-track", 16000, 0, 320)]
    original = [probe.fixture_frame(), probe.fixture_frame(value=200)]
    replacement = [original[0], probe.fixture_frame(value=0)]
    assert original[1].data.tobytes() != replacement[1].data.tobytes()
    expected = probe.observe(
        segments, original, InboundRtpStreamStats(total_samples_received=320, concealed_samples=0),
    )
    substituted = probe.observe(
        segments, replacement,
        InboundRtpStreamStats(total_samples_received=320, concealed_samples=160),
    )
    assert expected["decoded_pcm_counts"] == substituted["decoded_pcm_counts"]
    assert expected["inbound_rtp_stats"]["concealed_samples"] == 0
    assert substituted["inbound_rtp_stats"]["concealed_samples"] == 160
    for result in (expected, substituted):
        assert result["source_pcm_offset_verified"] is False
        assert result["ack_eligible"] is False


def test_fixture_marks_post_cancel_arrival_without_ack():
    result = probe.report()["cases"]["cancel_then_late_frame"]
    assert result["decoded_pcm_counts"][0]["samples_per_channel"] == 320
    assert result["received_after_cancel"] == 1
    assert result["source_pcm_offset_verified"] is False
    assert result["ack_eligible"] is False


def test_equal_duration_at_different_rates_and_missing_stats_are_not_mapping():
    result = probe.report()["cases"]["receiver_rate_change"]
    submitted = result["submitted_segments"]
    source_duration = sum(s["sample_end"] - s["sample_start"] for s in submitted) / 16000
    decoded = result["decoded_pcm_counts"][0]
    assert source_duration == decoded["samples_per_channel"] / decoded["sample_rate"]
    assert decoded["sample_rate"] == 48000
    assert result["inbound_rtp_stats"]["concealed_samples"] is None
    assert result["source_pcm_offset_verified"] is False
    assert result["ack_eligible"] is False


def test_cli_is_explicitly_synthetic_without_room_source_or_socket(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("オフライン観測で実行しない")

    monkeypatch.setattr(rtc, "Room", forbidden)
    monkeypatch.setattr(rtc, "AudioSource", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    probe.main()
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline_synthetic"
    assert result["numeric_values_are_fixtures"] is True
    assert result["connection_attempted"] is False
    assert result["fixture_limits"]["inbound_rtp_stats_clock_rate_hz"] is None
    assert result["fixture_limits"]["codec_observed"] is False
    assert result["sdk_boundary"]["livekit_version"] == "1.1.16"
    assert "data_ptr" in result["sdk_boundary"]["audio_frame_buffer_fields"]
    for case in result["cases"].values():
        assert case["browser_output_clock_observed"] is False
        assert case["source_pcm_offset_verified"] is False
        assert case["ack_eligible"] is False
