"""host制御wireの検証と、公開可能な固定reasonへの変換。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TypeGuard

from .sent_audio import SentAudioSnapshot

MAX_CONTROL_BYTES = 8192
MAX_SAFE_INTEGER = (1 << 53) - 1


class RpcRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def identifier(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeError:
        return False


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RpcRejected("invalid_control")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise RpcRejected("invalid_control")


@dataclass(frozen=True)
class Control:
    kind: str
    session_id: str
    binding: str
    request_id: str
    track_sid: str | None = None
    expected_revision: int | None = None
    grant: dict[str, object] | None = None
    response_id: str | None = None
    text: str | None = None
    enabled: bool | None = None


class InvalidInputAck(RpcRejected):
    def __init__(self, session_id: str, binding: str, grant: object) -> None:
        super().__init__("invalid_control")
        self.session_id, self.binding, self.grant = session_id, binding, grant


def parse_control(payload: str) -> Control:
    try:
        if not 0 < len(payload.encode("utf-8")) <= MAX_CONTROL_BYTES:
            raise RpcRejected("invalid_control")
        raw: object = json.loads(payload, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(raw, dict):
            raise RpcRejected("invalid_control")
        kind = raw.get("type")
        fields = {
            "open_input": {"track_sid", "expected_revision"},
            "input_ack": {"grant"},
            "confirm_output_ready": {"response_id", "track_sid"},
            "cancel": {"response_id"}, "text": {"text"},
            "mute": {"enabled"}, "focus": {"enabled"}, "reconnect": set(), "close": set(),
        }
        if not isinstance(kind, str) or kind not in fields:
            raise RpcRejected("invalid_control")
        if (set(raw) != {"v", "type", "session_id", "binding", "request_id"} | fields[kind]
                or type(raw["v"]) is not int or raw["v"] != 1):
            raise RpcRejected("invalid_control")
        for key in ("session_id", "binding", "request_id", "track_sid", "response_id"):
            if key in raw and not identifier(raw[key]):
                raise RpcRejected("invalid_control")
        revision = raw.get("expected_revision")
        if kind == "open_input" and (
            type(revision) is not int or not 0 <= revision < MAX_SAFE_INTEGER
        ):
            raise RpcRejected("invalid_control")
        grant = raw.get("grant")
        if kind == "input_ack":
            if (not isinstance(grant, dict) or set(grant) != {
                "track_sid", "request_id", "input_revision", "input_generation",
            }):
                raise InvalidInputAck(raw["session_id"], raw["binding"], grant)
            if (not identifier(grant["track_sid"]) or not identifier(grant["request_id"])
                    or any(type(grant[key]) is not int or not 0 < grant[key] <= MAX_SAFE_INTEGER
                           for key in ("input_revision", "input_generation"))):
                raise InvalidInputAck(raw["session_id"], raw["binding"], grant)
        if kind == "text" and (not isinstance(raw["text"], str) or not raw["text"].strip()):
            raise RpcRejected("invalid_control")
        if kind in {"mute", "focus"} and type(raw["enabled"]) is not bool:
            raise RpcRejected("invalid_control")
        return Control(kind, raw["session_id"], raw["binding"], raw["request_id"],
                       raw.get("track_sid"), revision, grant, raw.get("response_id"),
                       raw.get("text"), raw.get("enabled"))
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise RpcRejected("invalid_control") from None


def rejected_result(error: BaseException) -> dict[str, object]:
    if isinstance(error, RpcRejected):
        reason = error.reason
    elif isinstance(error, TimeoutError):
        reason = "input_timeout"
    elif isinstance(error, asyncio.CancelledError):
        reason = "operation_invalidated"
    else:
        reason = "operation_failed"
    return {"ok": False, "reason": reason}


def estimate_fields(snapshot: SentAudioSnapshot) -> dict[str, object]:
    return {
        "response_id": snapshot.response_id, "generation": snapshot.generation,
        "track_sid": snapshot.track_sid, "sample_rate": snapshot.sample_rate,
        "at_ns": snapshot.at_ns, "effective_at_ns": snapshot.effective_at_ns,
        "frozen_at_ns": snapshot.frozen_at_ns,
        "submitted_sample_end": snapshot.submitted_sample_end,
        "estimated_sample_end": snapshot.estimated_sample_end,
        "estimated_audio_sequence": snapshot.estimated_audio_sequence,
        "estimated_complete": snapshot.estimated_complete,
        "basis": snapshot.basis, "real_playback_confirmed": snapshot.real_playback_confirmed,
    }
