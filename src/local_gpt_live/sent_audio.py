"""SDK投入範囲とBE時計だけから求める推定。実再生ACKの状態は持たない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_RATES = frozenset({16000, 22050, 24000, 44100, 48000})
_SECOND_NS = 1_000_000_000


def _integer(value: int, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _identifier(value: str) -> bool:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeError:
        return False


@dataclass(frozen=True)
class SentAudioScope:
    response_id: str
    generation: int
    track_sid: str
    sample_rate: int


@dataclass(frozen=True)
class SentAudioBlock:
    """範囲は全て半開区間。部分範囲の開始はsample_startと共通。"""

    audio_sequence: int
    sample_start: int
    sample_end: int
    submitted_sample_end: int
    estimated_sample_end: int
    first_submitted_at_ns: int | None
    last_submitted_at_ns: int | None


@dataclass(frozen=True)
class SentAudioSnapshot:
    response_id: str
    generation: int
    track_sid: str
    sample_rate: int
    at_ns: int
    effective_at_ns: int
    frozen_at_ns: int | None
    submitted_sample_end: int
    estimated_sample_end: int
    estimated_audio_sequence: int
    estimated_complete: bool
    blocks: tuple[SentAudioBlock, ...]
    basis: Literal["sdk_submitted_elapsed"] = "sdk_submitted_elapsed"
    real_playback_confirmed: Literal[False] = False


@dataclass(frozen=True)
class _Block:
    sequence: int
    start: int
    end: int


@dataclass(frozen=True)
class _Frame:
    sequence: int
    start: int
    end: int
    completed_at_ns: int
    estimated_end_ns: int


class SentAudioProgress:
    """単一応答の有限台帳。時刻は同一BEのmonotonic_nsを明示的に渡す。

    snapshotは過去を照会しても台帳を進めない。block計画には登録時刻を
    持たないため、過去照会にも全登録済み計画が現れる。投入値だけが時刻で絞られる。
    """

    def __init__(self, *, downlink_delay_ns: int = 300_000_000,
                 sdk_queue_allowance_ns: int = 100_000_000,
                 max_records: int = 20_000, max_blocks: int = 256) -> None:
        if (not _integer(downlink_delay_ns) or not _integer(sdk_queue_allowance_ns)
                or not _integer(max_records, minimum=1)
                or not _integer(max_blocks, minimum=1)):
            raise ValueError("invalid_sent_audio_config")
        self._delay_ns = downlink_delay_ns + sdk_queue_allowance_ns
        self._max_records, self._max_blocks = max_records, max_blocks
        self._scope: SentAudioScope | None = None
        self._blocks: list[_Block] = []
        self._frames: list[_Frame] = []
        self._records: dict[tuple[int, int, int], _Frame] = {}
        self._submitted_end = 0
        self._frozen_at_ns: int | None = None
        self._frozen_snapshot: SentAudioSnapshot | None = None

    def begin(self, *, response_id: str, generation: int, track_sid: str,
              sample_rate: int) -> SentAudioScope:
        if (not _identifier(response_id) or not _identifier(track_sid)
                or not _integer(generation) or not _integer(sample_rate)
                or sample_rate not in _RATES):
            raise ValueError("invalid_sent_audio_scope")
        self._scope = SentAudioScope(response_id, generation, track_sid, sample_rate)
        self._blocks.clear()
        self._frames.clear()
        self._records.clear()
        self._submitted_end = 0
        self._frozen_at_ns = None
        self._frozen_snapshot = None
        return self._scope

    def _current(self, scope: SentAudioScope) -> bool:
        return self._scope is not None and scope is self._scope and self._frozen_at_ns is None

    def begin_block(self, *, scope: SentAudioScope, audio_sequence: int,
                    sample_start: int, sample_end: int) -> bool:
        if not self._current(scope):
            return False
        if (not _integer(audio_sequence) or not _integer(sample_start)
                or not _integer(sample_end) or sample_end <= sample_start):
            raise ValueError("invalid_sent_audio_block")
        block = _Block(audio_sequence, sample_start, sample_end)
        if audio_sequence < len(self._blocks) and self._blocks[audio_sequence] == block:
            return True
        if (audio_sequence != len(self._blocks)
                or sample_start != (self._blocks[-1].end if self._blocks else 0)):
            raise ValueError("noncontiguous_sent_audio_block")
        if len(self._blocks) >= self._max_blocks:
            raise ValueError("sent_audio_block_capacity_exceeded")
        self._blocks.append(block)
        return True

    def record(self, *, scope: SentAudioScope, audio_sequence: int,
               sample_start: int, sample_end: int, completed_at_ns: int) -> bool:
        if not self._current(scope):
            return False
        if (not _integer(audio_sequence) or not _integer(sample_start)
                or not _integer(sample_end) or sample_end <= sample_start
                or not _integer(completed_at_ns)):
            raise ValueError("invalid_sent_audio_record")
        key = (audio_sequence, sample_start, sample_end)
        if key in self._records:
            # 通知の再送で元の投入時刻を遅らせたり、履歴を二重計上したりしない。
            return True
        if audio_sequence >= len(self._blocks):
            raise ValueError("unplanned_sent_audio_block")
        block = self._blocks[audio_sequence]
        if (sample_start != self._submitted_end or sample_start < block.start
                or sample_end > block.end):
            raise ValueError("noncontiguous_sent_audio_record")
        previous = self._frames[-1] if self._frames else None
        if previous is not None and completed_at_ns < previous.completed_at_ns:
            raise ValueError("sent_audio_clock_regressed")
        if len(self._frames) >= self._max_records:
            raise ValueError("sent_audio_record_capacity_exceeded")
        # 端数nsは切り上げる。架空の即時SDKでも音声時間より速く推定を進めない。
        duration_ns = ((sample_end - sample_start) * _SECOND_NS + scope.sample_rate - 1
                       ) // scope.sample_rate
        start_ns = max(completed_at_ns, previous.estimated_end_ns if previous else 0)
        frame = _Frame(audio_sequence, sample_start, sample_end, completed_at_ns,
                       start_ns + duration_ns)
        self._frames.append(frame)
        self._records[key] = frame
        self._submitted_end = sample_end
        return True

    def next_estimated_complete_at_ns(self) -> int | None:
        """登録済み全blockの推定完了期限。生成・応答の完了は判定しない。

        timer発火時も現行scope、生成状態、全予定blockを呼出側で再照合する。
        新blockの追加や凍結で、以前取得した期限は失効しうる。
        """
        if (self._scope is None or self._frozen_at_ns is not None
                or not self._blocks or not self._frames
                or self._submitted_end != self._blocks[-1].end):
            return None
        return self._frames[-1].estimated_end_ns + self._delay_ns

    def snapshot(self, at_ns: int) -> SentAudioSnapshot:
        if not _integer(at_ns):
            raise ValueError("invalid_sent_audio_query_time")
        scope = self._scope
        if scope is None:
            raise ValueError("sent_audio_scope_unavailable")
        effective = at_ns if self._frozen_at_ns is None else min(at_ns, self._frozen_at_ns)
        cutoff = effective - self._delay_ns
        submitted_end = estimated_end = 0
        first: dict[int, int] = {}
        last: dict[int, int] = {}
        for frame in self._frames:
            if frame.completed_at_ns > effective:
                break
            submitted_end = frame.end
            first.setdefault(frame.sequence, frame.completed_at_ns)
            last[frame.sequence] = frame.completed_at_ns
            if frame.estimated_end_ns <= cutoff:
                estimated_end = frame.end
        blocks = tuple(SentAudioBlock(
            block.sequence, block.start, block.end,
            max(block.start, min(block.end, submitted_end)),
            max(block.start, min(block.end, estimated_end)),
            first.get(block.sequence), last.get(block.sequence),
        ) for block in self._blocks)
        estimated_sequence = -1
        for block in blocks:
            if block.estimated_sample_end < block.sample_end:
                break
            estimated_sequence = block.audio_sequence
        return SentAudioSnapshot(
            scope.response_id, scope.generation, scope.track_sid, scope.sample_rate,
            at_ns, effective, self._frozen_at_ns, submitted_end, estimated_end,
            estimated_sequence, bool(blocks) and estimated_sequence == len(blocks) - 1, blocks,
        )

    def freeze(self, at_ns: int) -> SentAudioSnapshot:
        """初回決定時刻で固定。過去時刻の固定も許し、再呼出しでは変化させない。"""
        if self._frozen_snapshot is not None:
            return self._frozen_snapshot
        # 検証してから失効する。過去の取消決定は後着recordを履歴照会から除外する。
        self.snapshot(at_ns)
        self._frozen_at_ns = at_ns
        self._frozen_snapshot = self.snapshot(at_ns)
        return self._frozen_snapshot
