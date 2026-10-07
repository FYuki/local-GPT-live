"""RTP の観測境界を確認する、接続しない SDK 合成ハーネス。

  uv run --no-sync python tools/rtp_observation.py

数値は実測ではなく fixture。AudioFrame の PCM と既存 SegmentSent を使うが、
encode/decode、RTP 配送、ブラウザの出力時計は実行しない。送受信の数量が一致しても
元 PCM 区間との対応を確定せず、再生 ACK は生成しない。音声本文やポインタ値は出さない。
inbound 統計の clock は不明。復号 PCM の rate との共通単位や同一時間幅を仮定しない。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from importlib.metadata import version

from livekit import rtc
from livekit.rtc._proto.stats_pb2 import InboundRtpStreamStats

from local_gpt_live.livekit_transport import SegmentSent


STAT_FIELDS = (
    "total_samples_received", "concealed_samples", "concealment_events",
    "inserted_samples_for_deceleration", "removed_samples_for_acceleration",
    "jitter_buffer_emitted_count", "estimated_playout_timestamp",
)


def observe(
    segments: Sequence[SegmentSent], frames: Sequence[rtc.AudioFrame],
    stats: InboundRtpStreamStats, *, cancel_after_frames: int | None = None,
) -> dict:
    """独立の fixture 列から送出 metadata・復号後 PCM・統計を対応付けずに集計する。"""
    counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    after_cancel = 0
    for index, frame in enumerate(frames):
        count = counts[frame.sample_rate, frame.num_channels]
        count[0] += 1
        count[1] += frame.samples_per_channel
        if cancel_after_frames is not None and index >= cancel_after_frames:
            after_cancel += 1
    return {
        "submitted_segments": [asdict(segment) for segment in segments],
        "decoded_pcm_counts": [
            {"sample_rate": rate, "num_channels": channels,
             "frames": count[0], "samples_per_channel": count[1]}
            for (rate, channels), count in sorted(counts.items())
        ],
        # 未設定の optional 統計を「欠落・補完なし」の 0 と解釈しない。
        "inbound_rtp_stats": {
            name: getattr(stats, name) if stats.HasField(name) else None
            for name in STAT_FIELDS
        },
        "received_after_cancel": after_cancel,
        "source_pcm_offset_verified": False,
        "ack_eligible": False,
        "browser_output_clock_observed": False,
        "reason": "source_to_rtp_origin_unavailable",
    }


def fixture_frame(samples: int = 160, *, value: int = 100, rate: int = 16000) -> rtc.AudioFrame:
    """合成 PCM のみ。補完や rate 変換そのものを SDK に実行させない。"""
    return rtc.AudioFrame(value.to_bytes(2, "little", signed=True) * samples,
                          rate, 1, samples)


def report() -> dict:
    segments = [SegmentSent("fixture-response", index, "fixture-track", 16000,
                            index * 160, (index + 1) * 160) for index in range(2)]
    frames = [fixture_frame(), fixture_frame(value=200)]
    continuous = InboundRtpStreamStats(total_samples_received=320, concealed_samples=0)
    substituted = InboundRtpStreamStats(
        total_samples_received=320, concealed_samples=160, concealment_events=1,
    )
    # _proto_info と _proto は固定 SDK 1.1.16 の境界を調べるためだけに使う。
    # data_ptr の値も SerializeToString() の結果も出力しない。
    return {
        "mode": "offline_synthetic",
        "numeric_values_are_fixtures": True,
        "connection_attempted": False,
        "fixture_limits": {
            "inbound_rtp_stats_clock_rate_hz": None,
            "codec_observed": False,
        },
        "sdk_boundary": {
            "livekit_version": version("livekit"),
            "audio_frame_buffer_fields": [
                field.name for field in frames[0]._proto_info().DESCRIPTOR.fields
            ],
            "selected_inbound_rtp_stats_fields": [
                name for name in STAT_FIELDS
                if name in InboundRtpStreamStats.DESCRIPTOR.fields_by_name
            ],
        },
        "cases": {
            "continuous": observe(segments, frames, continuous),
            "equal_count_substitution": observe(
                segments, [frames[0], fixture_frame(value=0)], substituted,
            ),
            "cancel_then_late_frame": observe(
                segments, frames, continuous, cancel_after_frames=1,
            ),
            "receiver_rate_change": observe(
                segments, [fixture_frame(960, rate=48000)],
                InboundRtpStreamStats(total_samples_received=960),
            ),
        },
    }


def main() -> None:
    print(json.dumps(report(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
