from __future__ import annotations

import struct

import pytest

from local_gpt_live.stt_audio import SttSignalSpan, prepare_stt_audio


def pcm(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def test_long_preroll_keeps_onset_margin_and_all_following_audio():
    quiet = [-1, 0, 16, -16] * 8000
    speech_and_pause = [17, -17, 3000, -3000] + [0] * 9600 + [2000, -2000] + [0] * 16000
    original = pcm(quiet + speech_and_pause)
    prepared, removed = prepare_stt_audio(original)
    assert removed == 32000 - 5120
    assert prepared == original[removed * 2:]
    assert prepared == pcm(quiet[-5120:] + speech_and_pause)


@pytest.mark.parametrize('values', [[], [0] * 40000, [16, -16] * 20000,
                                   [0] * 5000 + [17], [17] + [0] * 40000,
                                   [0] * 100 + [-17] + [0] * 40000])
def test_short_preroll_and_entirely_quiet_input_are_unchanged(values):
    original = pcm(values)
    assert prepare_stt_audio(original) == (original, 0)


def test_truncated_pcm_is_rejected():
    with pytest.raises(ValueError, match='complete PCM16'):
        prepare_stt_audio(b'\x00')


def test_preview_span_ignores_quiet_prefix_but_retains_internal_pause():
    span = SttSignalSpan()
    audio = bytearray(pcm([0, 16, -16] * 16000))
    assert span.sample_count(audio) == 0
    audio.extend(pcm([-17, 0, 0, 17]))
    assert span.sample_count(audio) == 4
    assert span.sample_count(audio) == 4
    audio.extend(pcm([0] * 1600))
    assert span.sample_count(audio) == 1604
    # 別発話の静音へ開始位置を持ち越さない。
    assert SttSignalSpan().sample_count(pcm([0] * 1600)) == 0


def test_preview_span_rejects_truncated_or_replaced_input():
    span = SttSignalSpan()
    assert span.sample_count(pcm([0, 17])) == 1
    with pytest.raises(ValueError, match="append-only complete PCM16"):
        span.sample_count(b"\x00")
    with pytest.raises(ValueError, match="append-only complete PCM16"):
        span.sample_count(pcm([17]))
