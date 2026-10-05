"""認証済み host RPC が渡す個別 ACK を、現在の再生 binding に限定する。"""

from __future__ import annotations

import json
from typing import TypeGuard
from uuid import uuid4

from .session import VoiceSession

MAX_WIRE_BYTES = 2048
MAX_ID_BYTES = 256
MAX_SAFE_INTEGER = (1 << 53) - 1
SAMPLE_RATES = frozenset({16000, 22050, 24000, 44100, 48000})


def _identifier(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_ID_BYTES
    except UnicodeError:
        return False


def _integer(value: object, minimum: int = 0) -> TypeGuard[int]:
    return type(value) is int and minimum <= value <= MAX_SAFE_INTEGER


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_wire_field")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("invalid_json_constant")


class PlaybackAckTransport:
    """接続一回分の受信境界。認証と再生事実の観測は host/browser が所有する。

    binding は認証資格情報ではない。host は response 開始時に一度 bind し、
    取消・切断時には invalidate を同期的に呼び出す。receive の真偽を RPC の
    受理結果として返し、SDK 送信成功だけで browser の ACK 再送を止めない。
    """

    def __init__(self, session: VoiceSession, participant_identity: str,
                 participant_sid: str) -> None:
        if not _identifier(participant_identity) or not _identifier(participant_sid):
            raise ValueError("invalid_playback_participant")
        self.session = session
        self.participant_identity, self.participant_sid = participant_identity, participant_sid
        self._closed = False
        self._binding: str | None = None
        self._response: str | None = None
        self._generation = -1
        self._segment: tuple[int, str, int, int, int] | None = None
        self._receipt: tuple[str, str, int] | None = None

    def bind(self, response_id: str) -> str:
        if (self._closed or not _identifier(response_id) or self.session.active != response_id
                or self.session.playback.active != response_id):
            raise ValueError("playback_binding_unavailable")
        self.invalidate()
        self._response = response_id
        self._generation = self.session.generation
        self._binding = uuid4().hex
        return self._binding

    def invalidate(self) -> None:
        """Session/provider の取消は呼出側が行い、ここでは受付を即失効する。"""
        self._binding = self._response = None
        self._generation = -1
        self._segment = self._receipt = None

    def close(self) -> None:
        self._closed = True
        self.invalidate()

    def _current(self, response_id: str) -> bool:
        return (not self._closed and self._binding is not None and self._response == response_id
                and self._generation == self.session.generation
                and self.session.active == response_id
                and self.session.playback.active == response_id)

    def record_segment(self, *, response_id: str, sequence: int, track_sid: str,
                       sample_rate: int, sample_start: int, sample_end: int) -> bool:
        """SDK 送出 callback の範囲を登録するだけで、再生 ACK は作らない。"""
        if (not self._current(response_id) or not _identifier(track_sid)
                or not _integer(sequence) or not _integer(sample_rate)
                or sample_rate not in SAMPLE_RATES or not _integer(sample_start)
                or not _integer(sample_end) or sample_end <= sample_start):
            return False
        segment = (sequence, track_sid, sample_rate, sample_start, sample_end)
        previous = self._segment
        if previous == segment:
            return True
        if previous is None:
            if sequence != 0 or sample_start != 0:
                return False
        elif (sequence != previous[0] + 1 or track_sid != previous[1]
              or sample_rate != previous[2] or sample_start != previous[4]):
            return False
        self._segment = segment
        return True

    def receive(self, payload: bytes, *, participant_identity: str, participant_sid: str) -> bool:
        if (self._closed or self._binding is None or not isinstance(payload, bytes)
                or not 0 < len(payload) <= MAX_WIRE_BYTES
                or participant_identity != self.participant_identity
                or participant_sid != self.participant_sid):
            return False
        try:
            message: object = json.loads(payload.decode("utf-8"), object_pairs_hook=_object,
                                         parse_constant=_constant)
        except (UnicodeError, ValueError, RecursionError):
            return False
        if not isinstance(message, dict):
            return False
        kind = message.get("type")
        if kind == "playback_ack":
            sequence_key, minimum = "audio_sequence", 0
        elif kind == "playback_complete":
            sequence_key, minimum = "final_audio_sequence", -1
        else:
            return False
        if set(message) != {"v", "type", "binding", "response_id", sequence_key}:
            return False
        binding, response_id, sequence = (message["binding"], message["response_id"],
                                           message[sequence_key])
        if (type(message["v"]) is not int or message["v"] != 1
                or not _identifier(binding) or binding != self._binding
                or not _identifier(response_id) or response_id != self._response
                or not _integer(sequence, minimum)
                or self._generation != self.session.generation):
            return False
        receipt = (binding, response_id, sequence)
        if (kind == "playback_complete" and self._receipt == receipt
                and self.session.active is None):
            return True
        if not self._current(response_id):
            return False
        last_sequence = self._segment[0] if self._segment is not None else -1
        if kind == "playback_ack":
            return (sequence <= last_sequence
                    and self.session.acknowledge_playback(response_id, sequence))
        if (sequence != last_sequence or self.session.generated != response_id
                or not self.session.playback.all_confirmed):
            return False
        if not self.session.playback_completed(response_id):
            return False
        self._receipt = receipt
        return True
