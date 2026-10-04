"""16kHz PCM16の長いほぼ無音のprerollを、語頭の余白を残して短くする。"""
from __future__ import annotations

import struct

# Opus復号後の量子化雑音を想定した約-66dBFS。VADの発話判定閾値には使わない。
_QUIET_PEAK_PCM16 = 16
_PREFIX_SAMPLES = 16_000 * 320 // 1000


def prepare_stt_audio(pcm: bytes) -> tuple[bytes, int]:
    """最初の閾値超過より前の320msと、以降の全sampleをそのまま保持する。"""
    if len(pcm) % 2:
        raise ValueError("STT input must contain complete PCM16 samples")
    for index, (sample,) in enumerate(struct.iter_unpack("<h", pcm)):
        if abs(sample) > _QUIET_PEAK_PCM16:
            removed = max(0, index - _PREFIX_SAMPLES)
            return pcm[removed * 2:], removed
    # 全体が小音量の場合は発話か無音かをここで決めず、元の入力を維持する。
    return pcm, 0


class SttSignalSpan:
    """追記されるPCMの最初の静音閾値超過以降の長さを、重複走査せず数える。"""

    def __init__(self) -> None:
        self._scanned_samples = 0
        self._first_signal_sample: int | None = None

    def sample_count(self, pcm: bytes | bytearray) -> int:
        samples = len(pcm) // 2
        if len(pcm) % 2 or samples < self._scanned_samples:
            raise ValueError("STT signal input must be append-only complete PCM16")
        if self._first_signal_sample is None:
            for offset, (value,) in enumerate(
                struct.iter_unpack("<h", memoryview(pcm)[self._scanned_samples * 2:])
            ):
                if abs(value) > _QUIET_PEAK_PCM16:
                    self._first_signal_sample = self._scanned_samples + offset
                    break
        self._scanned_samples = samples
        if self._first_signal_sample is None:
            return 0
        return samples - self._first_signal_sample


class PcmCaptureSpan:
    """受信PCMの連番上で切り出した範囲だけを保持する。波形・受信時計は保存しない。"""

    def __init__(self) -> None:
        self._start_byte: int | None = None
        self._end_byte: int | None = None
        self._bytes = 0
        self._valid = True

    def append(self, start_byte: int, byte_count: int) -> None:
        if (type(start_byte) is not int or type(byte_count) is not int
                or start_byte < 0 or byte_count < 0 or start_byte % 2 or byte_count % 2):
            self._valid = False
            return
        if byte_count == 0:
            return
        if self._end_byte is not None and start_byte != self._end_byte:
            self._valid = False
        if self._start_byte is None:
            self._start_byte = start_byte
        self._end_byte = start_byte + byte_count
        self._bytes += byte_count

    def statistics(self, captured_pcm: bytes | bytearray) -> dict[str, int]:
        valid = (self._valid and self._start_byte is not None and self._end_byte is not None
                 and len(captured_pcm) == self._bytes
                 and self._end_byte - self._start_byte == self._bytes)
        result = {'stt_capture_received_span_valid': int(valid),
                  'stt_capture_raw_sample_count': len(captured_pcm) // 2}
        if valid and self._start_byte is not None and self._end_byte is not None:
            result['stt_capture_received_start_sample'] = self._start_byte // 2
            result['stt_capture_received_end_sample'] = self._end_byte // 2
        return result


def stt_preparation_statistics(original: bytes, prepared: bytes, removed_samples: int) -> dict[str, int]:
    """実際に渡すバイト列が宣言したprefix除去後の全suffixと一致するか照合する。"""
    valid = (type(removed_samples) is int and removed_samples >= 0
             and len(original) % 2 == 0 and len(prepared) % 2 == 0
             and removed_samples <= len(original) // 2
             and memoryview(original)[removed_samples * 2:] == prepared)
    return {'stt_input_raw_sample_count': len(original) // 2,
            'stt_input_prepared_sample_count': len(prepared) // 2,
            'stt_input_removed_prefix_samples': removed_samples,
            'stt_input_suffix_preserved': int(valid)}
