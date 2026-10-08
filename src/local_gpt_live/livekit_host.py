"""単一の認証済みparticipantとSessionに公式SDK RPCを束縛する。"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from uuid import uuid4

from livekit import rtc

from .host_rpc import (
    Control,
    InvalidInputAck,
    RpcRejected,
    estimate_fields,
    identifier,
    parse_control,
    parse_state_request,
    rejected_result,
)
from .livekit_transport import LiveKitTransport, SegmentSent, TrackPublished
from .playback_ack_transport import PlaybackAckTransport
from .session import Event
from .voice_input.session import InputGrant

CONTROL_METHOD = "local-gpt-live.control.v1"
ACK_METHOD = "local-gpt-live.playback-ack.v1"
STATE_METHOD = "local-gpt-live.state.v1"
NOTIFICATION_TOPIC = "local-gpt-live.events.v1"
INPUT_ACK_SECONDS = 5.0


@dataclass
class _InputOperation:
    command: Control
    deadline_ns: int
    task: asyncio.Task[InputGrant]
    grant: InputGrant | None = None
    timer: asyncio.TimerHandle | None = None
    started: bool = False


class LiveKitHost:
    def __init__(self, transport: LiveKitTransport, *, session_id: str,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if not identifier(session_id):
            raise ValueError("invalid_host_session")
        self.transport = transport
        self.session = transport.audio.session
        self.session_id = session_id
        self._clock_ns = clock_ns
        self.binding = uuid4().hex
        self.connection_id = uuid4().hex
        self._state_sequence = 0
        config = transport.config
        self._acks = PlaybackAckTransport(self.session, config.participant_identity,
                                          config.participant_sid)
        self._output_binding: str | None = None
        self._input: _InputOperation | None = None
        self._muted = self._focused = self._closed = False
        self._methods: list[str] = []
        self._closing: asyncio.Task[None] | None = None
        self._operations: set[asyncio.Task[InputGrant]] = set()
        self._notifications: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue(512)
        self._notifying: asyncio.Task[None] | None = None
        transport.attach_host(on_track=self._track, on_segment=self._segment,
                              on_event=self._event, on_lost=self._lost)

    def _authorized(self) -> bool:
        config = self.transport.config
        participant = self.transport.room.remote_participants.get(config.participant_identity)
        return (not self._closed and self.transport.room.isconnected()
                and participant is not None
                and participant.identity == config.participant_identity
                and participant.sid == config.participant_sid)

    def _authenticate(self, data: rtc.RpcInvocationData) -> None:
        if (not self._check_authorized()
                or data.caller_identity != self.transport.config.participant_identity):
            raise RpcRejected("unauthorized")

    def _check_authorized(self) -> bool:
        if self._authorized():
            return True
        self._lost(stop_session=True)
        return False

    async def connect(self) -> None:
        try:
            await self.transport.connect()
            if not self._authorized():
                raise RpcRejected("unauthorized")
            participant = self.transport.room.local_participant
            for method, handler in ((CONTROL_METHOD, self._control), (ACK_METHOD, self._ack),
                                    (STATE_METHOD, self._get_state)):
                # SDKはFFI登録前にhandlerを保存するため、部分失敗も回収対象にする。
                self._methods.append(method)
                participant.register_rpc_method(method, handler)
            self._notifying = asyncio.create_task(self._notify())
            self._queue({"v": 1, "type": "host_state"})
        except BaseException:
            await self.aclose()
            raise RuntimeError("host_connection_failed") from None

    async def _control(self, data: rtc.RpcInvocationData) -> str:
        owned_session = False
        try:
            self._authenticate(data)
            command = parse_control(data.payload)
            if command.session_id != self.session_id:
                raise RpcRejected("stale_binding")
            owned_session = True
            if command.binding != self.binding:
                raise RpcRejected("stale_binding")
            result = await self._execute(command, data.response_timeout)
        except (Exception, asyncio.CancelledError) as error:
            if owned_session:
                self._check_authorized()
            if (isinstance(error, InvalidInputAck) and error.session_id == self.session_id
                    and error.binding == self.binding):
                self._reject_grant(error.grant)
            result = rejected_result(error)
        if owned_session:
            result.update(self._state())
            result["binding"] = self.binding
        return json.dumps(result, allow_nan=False)

    async def _get_state(self, data: rtc.RpcInvocationData) -> str:
        try:
            self._authenticate(data)
            session_id, connection_id = parse_state_request(data.payload)
            if session_id != self.session_id or connection_id != self.connection_id:
                raise RpcRejected("stale_binding")
            result = {"ok": True, "v": 1, "type": "host_state", **self._state()}
        except (Exception, asyncio.CancelledError) as error:
            result = rejected_result(error)
        return json.dumps(result, allow_nan=False)

    def _state(self) -> dict[str, object]:
        self._state_sequence += 1
        return {"session_id": self.session_id, "connection_id": self.connection_id,
                "state_sequence": self._state_sequence, "control_binding": self.binding,
                "input_revision": self.transport.audio.backend.revision,
                "input_active": self.transport.audio.backend.grant is not None,
                "muted": self._muted, "focused": self._focused,
                "active_response_id": self.session.active, "closed": self._closed}

    async def _ack(self, data: rtc.RpcInvocationData) -> str:
        try:
            self._authenticate(data)
            accepted = self._acks.receive(
                data.payload.encode("utf-8"),
                participant_identity=data.caller_identity,
                participant_sid=self.transport.config.participant_sid,
            )
            result: dict[str, object] = (
                {"ok": True} if accepted else {"ok": False, "reason": "playback_ack_rejected"}
            )
        except (Exception, asyncio.CancelledError) as error:
            result = rejected_result(error)
        return json.dumps(result, allow_nan=False)

    def _invalidate_input(self, reason: str) -> None:
        operation, self._input = self._input, None
        if operation is not None:
            if operation.timer is not None:
                operation.timer.cancel()
            operation.task.cancel()
        self.transport.input.stop(reason=reason)

    def _invalidate_output(self) -> None:
        self._acks.invalidate()
        self._output_binding = None
        self.binding = uuid4().hex
        while not self._notifications.empty():
            self._notifications.get_nowait()
            self._notifications.task_done()

    def _expire_input(self, operation: _InputOperation) -> None:
        if self._input is operation and not operation.started:
            self._invalidate_input("input_timeout")

    async def _open(self, command: Control, budget: float) -> dict[str, object]:
        if self._muted or self._focused:
            raise RpcRejected("input_suppressed")
        operation = self._input
        if operation is not None:
            if command != operation.command:
                raise RpcRejected("input_conflict")
        else:
            if command.expected_revision != self.transport.audio.backend.revision:
                raise RpcRejected("stale_revision")
            if not math.isfinite(budget) or budget <= 0:
                raise RpcRejected("input_timeout")
            assert command.track_sid is not None
            duration = min(INPUT_ACK_SECONDS, budget)
            task = asyncio.create_task(self.transport.prepare_input(
                track_sid=command.track_sid, request_id=command.request_id,
                revision=self.transport.audio.backend.revision + 1,
            ))
            operation = _InputOperation(command, self._clock_ns() + round(duration * 1e9), task)
            self._input = operation
            self._operations.add(task)
            task.add_done_callback(self._observe_operation)
            operation.timer = asyncio.get_running_loop().call_later(
                duration, self._expire_input, operation,
            )
        try:
            remaining = (operation.deadline_ns - self._clock_ns()) / 1e9
            done, _ = await asyncio.wait([operation.task], timeout=max(0, remaining))
            if not done or self._clock_ns() >= operation.deadline_ns:
                raise TimeoutError
            grant = operation.task.result()
            if (self._input is not operation or not self._authorized()
                    or self.transport.audio.backend.grant is not grant):
                raise RpcRejected("operation_invalidated")
            operation.grant = grant
            return {"ok": True, "grant": asdict(grant), "binding": self.binding,
                    "remaining_ms": max(0, (operation.deadline_ns - self._clock_ns()) // 1_000_000)}
        except BaseException:
            if self._input is operation:
                if (operation.task.done()
                        and self.transport.audio.backend.revision == command.expected_revision):
                    self._input = None
                    if operation.timer is not None:
                        operation.timer.cancel()
                else:
                    self._invalidate_input("input_open_failed")
            raise

    def _observe_operation(self, task: asyncio.Task[InputGrant]) -> None:
        self._operations.discard(task)
        if not task.cancelled():
            task.exception()

    def _input_ack(self, command: Control) -> dict[str, object]:
        operation = self._input
        if operation is None or operation.grant is None:
            raise RpcRejected("stale_grant")
        grant = operation.grant
        assert command.grant is not None
        if command.grant != asdict(grant):
            # 同じ準備操作への不正ACKだけを失効させ、旧ACKで新grantを停止しない。
            self._reject_grant(command.grant)
            raise RpcRejected("stale_grant")
        if not operation.started and self._clock_ns() >= operation.deadline_ns:
            self._invalidate_input("input_timeout")
            raise RpcRejected("input_timeout")
        was_started = operation.started
        if not self.transport.start_input(grant):
            self._invalidate_input("input_start_failed")
            raise RpcRejected("input_start_failed")
        if not was_started and self._clock_ns() >= operation.deadline_ns:
            self._invalidate_input("input_timeout")
            raise RpcRejected("input_timeout")
        operation.started = True
        if operation.timer is not None:
            operation.timer.cancel()
        return {"ok": True}

    def _reject_grant(self, claimed: object) -> None:
        operation = self._input
        if operation is None or operation.grant is None:
            return
        revision = claimed.get("input_revision") if isinstance(claimed, dict) else None
        if type(revision) is int and revision < operation.grant.input_revision:
            return
        self._invalidate_input("grant_rejected")

    async def _execute(self, command: Control, budget: float) -> dict[str, object]:
        kind = command.kind
        if kind == "open_input":
            return await self._open(command, budget)
        if kind == "input_ack":
            return self._input_ack(command)
        if kind == "confirm_output_ready":
            assert command.response_id is not None and command.track_sid is not None
            if not self.transport.confirm_output_ready(command.response_id, command.track_sid):
                raise RpcRejected("output_not_ready")
            return {"ok": True}
        if kind in {"mute", "focus"}:
            assert command.enabled is not None
            previous = self._muted if kind == "mute" else self._focused
            if previous == command.enabled:
                return {"ok": True}
            if kind == "mute":
                self._muted = command.enabled
            else:
                self._focused = command.enabled
            if command.enabled:
                self._invalidate_input(kind)
            return {"ok": True}
        if kind == "cancel" and command.response_id != self.session.active:
            raise RpcRejected("stale_response")
        self._invalidate_input(kind)
        self._invalidate_output()
        if kind == "text":
            assert command.text is not None
            response_id = self.session.submit_text(command.text)
            return {"ok": True, "response_id": response_id, "binding": self.binding}
        if kind == "cancel":
            self.session.cancel()
        elif kind == "reconnect":
            self.session.reconnect()
        elif kind == "close":
            self._closed = True
            self._acks.close()
            self.session.cancel("closed")
            self._closing = asyncio.create_task(self._shutdown())
        return {"ok": True, "binding": self.binding}

    def _track(self, value: TrackPublished) -> None:
        if not self._check_authorized() or self.session.active != value.response_id:
            return
        binding = self._acks.bind(value.response_id)
        self._output_binding = binding
        self._queue({"v": 1, "type": "output_track", "response_id": value.response_id,
                     "track_sid": value.track_sid, "binding": binding,
                     "control_binding": self.binding})

    def _segment(self, value: SegmentSent) -> None:
        if not self._check_authorized() or self._output_binding is None:
            return
        if self._acks.record_segment(**asdict(value)):
            self._queue({"v": 1, "type": "segment_sent", **asdict(value),
                         "binding": self._output_binding})

    def _event(self, value: Event) -> None:
        if self._closed:
            return
        if value.kind == "response_started":
            self._invalidate_output()
            self._queue({"v": 1, "type": value.kind, "response_id": value.response_id})
            return
        if value.kind == "input_invalidated":
            operation, self._input = self._input, None
            if operation is not None:
                if operation.timer is not None:
                    operation.timer.cancel()
                operation.task.cancel()
        if value.kind in {"input_invalidated", "input_rejected", "input_failed",
                          "response_failed", "transport_failed"}:
            # 配送不能の通知を再通知して無限に再帰させない。
            if value.kind == "transport_failed" and value.detail in {
                    "notification_failed", "notification_capacity_exceeded"}:
                return
            self._queue({"v": 1, "type": value.kind, "response_id": value.response_id,
                         "reason": value.kind})
            return
        if value.kind == "generation_completed":
            self._queue({"v": 1, "type": value.kind, "response_id": value.response_id,
                         "final_audio_sequence": self.session.playback.last_audio_sequence})
            return
        if value.kind not in {"output_estimated_completed", "output_estimated_stopped",
                               "playback_stopped", "response_cancelled"}:
            return
        self._acks.invalidate()
        self._output_binding = None
        message: dict[str, object] = {"v": 1, "type": value.kind, "response_id": value.response_id}
        estimate = self.session.last_output_estimate
        if (value.kind.startswith("output_estimated_") and estimate is not None
                and estimate.response_id == value.response_id):
            message.update(estimate_fields(estimate))
        self._queue(message)

    def _queue(self, message: dict[str, object]) -> None:
        if not self._check_authorized():
            return
        message = {**message, **self._state()}
        try:
            self._notifications.put_nowait((self.binding, message))
        except asyncio.QueueFull:
            self.session.emit(Event("transport_failed", detail="notification_capacity_exceeded"))

    async def _notify(self) -> None:
        while not self._closed:
            binding, message = await self._notifications.get()
            try:
                if self._check_authorized() and binding == self.binding:
                    await self.transport.room.local_participant.publish_data(
                        json.dumps(message, allow_nan=False), reliable=True,
                        destination_identities=[self.transport.config.participant_identity],
                        topic=NOTIFICATION_TOPIC,
                    )
            except Exception:
                self.session.emit(Event("transport_failed", detail="notification_failed"))
            finally:
                self._notifications.task_done()

    def _lost(self, *, stop_session: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        # 認可対象の入力がないSID拒否では、Backendのrevisionを更新しない。
        if (not stop_session or self._input is not None
                or self.transport.audio.backend.grant is not None):
            self._invalidate_input("transport_disconnected")
        self._invalidate_output()
        self._acks.close()
        # SDK切断ではtransportが同期reconnectを所有する。SID喪失時だけここで停止する。
        if stop_session:
            self.session.cancel("transport_disconnected")
        if self._closing is None:
            self._closing = asyncio.create_task(self._shutdown())

    async def _shutdown(self) -> None:
        if self._methods:
            methods, self._methods = self._methods, []
            for method in methods:
                try:
                    self.transport.room.local_participant.unregister_rpc_method(method)
                except Exception:
                    self.session.emit(Event("transport_failed", detail="rpc_unregister_failed"))
        owned: list[asyncio.Task[object]] = list(self._operations)
        if self._notifying is not None:
            self._notifying.cancel()
            owned.append(self._notifying)
        if owned:
            _, pending = await asyncio.wait(owned, timeout=self.transport.config.close_timeout)
            if pending:
                self.session.emit(Event("shutdown_pending", detail="host_operations_not_drained"))
        await self.transport.aclose()

    async def aclose(self) -> None:
        if self._closing is None:
            self._closed = True
            self._invalidate_input("host_closed")
            self._invalidate_output()
            self._acks.close()
            self.session.cancel("closed")
            self._closing = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._closing)
