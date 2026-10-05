"""STT→Core API→TTSの取消・かぶせ制御。会話本文を永続化しない。"""

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .playback import AudioPacket, Playback
from .stt_audio import prepare_stt_audio
from .turn_decision import classify_turn


class Transcriber(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...


class Core(Protocol):
    def stream(self, text: str) -> AsyncIterator[str]: ...


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


@dataclass(frozen=True)
class Event:
    kind: str
    response_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Input:
    generation: int
    pcm: bytes
    overlap: str | None


class VoiceSession:
    def __init__(
        self, stt: Transcriber, core: Core, tts: Synthesizer, playback: Playback,
        *, emit: Callable[[Event], None] = lambda event: None,
        response_timeout: float = 60, stt_timeout: float = 50,
    ) -> None:
        if any(not math.isfinite(t) or t <= 0 for t in (response_timeout, stt_timeout)):
            raise ValueError("invalid_timeout")
        self.stt, self.core, self.tts, self.playback = stt, core, tts, playback
        self.emit = emit
        self.response_timeout, self.stt_timeout = response_timeout, stt_timeout
        self.generation = 0
        self.active: str | None = None
        self.generated: str | None = None
        self._closed = False
        self._inputs: asyncio.Queue[Input] = asyncio.Queue(maxsize=3)
        self._input_task: asyncio.Task[None] | None = None
        self._responses: dict[str, asyncio.Task[None]] = {}
        self._stt_lock = asyncio.Lock()

    @property
    def can_preview(self) -> bool:
        return not self._closed and not self._stt_lock.locked() and self._inputs.empty()

    async def preview(self, pcm: bytes, *, generation: int, overlap: str,
                      is_current: Callable[[], bool]) -> bool:
        """部分認識で旧回答を停止するが最終発話の回答はまだ作らない。"""
        if not self.can_preview or generation != self.generation or not is_current():
            return False
        try:
            async with self._stt_lock, asyncio.timeout(self.stt_timeout):
                deadline = asyncio.get_running_loop().time() + self.stt_timeout
                text = await self.stt.transcribe(prepare_stt_audio(pcm)[0])
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
            if (generation != self.generation or not is_current()
                    or self.active != overlap or self._closed):
                return False
            decision = classify_turn(text)
            self.emit(Event("turn_preview", overlap, decision))
            if decision == "take_turn":
                self._cancel_response("take_turn")
                return True
        except asyncio.CancelledError:
            raise
        except Exception:
            if generation == self.generation and is_current():
                self.emit(Event("preview_failed", overlap, "stt_failed"))
        return False

    def submit_audio(self, pcm: bytes, *, generation: int, overlap: str | None) -> bool:
        """正式VAD終了後のPCMのみ。overlapは発話開始時のresponseを渡す。"""
        if self._closed or generation != self.generation:
            return False
        if not pcm or len(pcm) % 2 or len(pcm) > 960_000:
            raise ValueError("invalid_or_oversized_pcm")
        try:
            self._inputs.put_nowait(Input(generation, pcm, overlap))
        except asyncio.QueueFull:
            self.emit(Event("input_rejected", detail="input_capacity_exceeded"))
            return False
        if self._input_task is None or self._input_task.done():
            self._input_task = asyncio.create_task(self._transcribe())
        return True

    async def _transcribe(self) -> None:
        while not self._inputs.empty() and not self._closed:
            item = self._inputs.get_nowait()
            try:
                async with self._stt_lock, asyncio.timeout(self.stt_timeout):
                    deadline = asyncio.get_running_loop().time() + self.stt_timeout
                    text = await self.stt.transcribe(prepare_stt_audio(item.pcm)[0])
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
                if item.generation != self.generation or self._closed:
                    continue
                if not text.strip():
                    self.emit(Event("input_ignored", detail="empty_transcript"))
                    continue
                if item.overlap is not None:
                    decision = classify_turn(text)
                    self.emit(Event("turn_decision", item.overlap, decision))
                    if decision != "take_turn":
                        continue
                self._start(text)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if item.generation == self.generation:
                    self.emit(Event("input_failed", detail="stt_timeout"))
            except Exception:
                if item.generation == self.generation:
                    self.emit(Event("input_failed", detail="stt_failed"))
            finally:
                self._inputs.task_done()

    def submit_text(self, text: str) -> str:
        if self._closed or not text.strip():
            raise ValueError("session_closed_or_empty_text")
        self._invalidate_input()
        return self._start(text)

    def _start(self, text: str) -> str:
        self._cancel_response("superseded")
        if len(self._responses) >= 4:
            raise ValueError("response_capacity_exceeded")
        response_id = str(uuid4())
        self.active = response_id
        self.generated = None
        self.playback.start(response_id)
        self.emit(Event("response_started", response_id))
        task = asyncio.create_task(self._respond(response_id, text))
        self._responses[response_id] = task
        # providerがCancelledErrorを抑止しても、期限で出力認可を先に失効させる。
        timer = asyncio.get_running_loop().call_later(self.response_timeout,
                                                      self._expire, response_id)

        def finished(done: asyncio.Task[None]) -> None:
            timer.cancel()
            self._responses.pop(response_id, None)
            if not done.cancelled() and done.exception() is not None:
                if self.active == response_id:
                    self._cancel_response("provider_failed")
                self.emit(Event("provider_task_failed", response_id))

        task.add_done_callback(finished)
        return response_id

    def _expire(self, response_id: str) -> None:
        if self.active == response_id and self.generated != response_id:
            self._cancel_response("response_timeout")
            self.emit(Event("response_failed", response_id, "response_timeout"))

    async def _respond(self, response_id: str, text: str) -> None:
        stream = self.core.stream(text)
        sequence, buffer, total = 0, "", 0

        async def speak(segment: str) -> None:
            nonlocal sequence
            if self.active != response_id:
                return
            wav = await self.tts.synthesize(segment)
            if self.active != response_id:
                return
            if self.playback.enqueue(AudioPacket(response_id, sequence, wav)):
                sequence += 1
                self.emit(Event("audio_queued", response_id))

        try:
            async with asyncio.timeout(self.response_timeout):
                async for chunk in stream:
                    if self.active != response_id:
                        return
                    total += len(chunk)
                    if total > 16_000:
                        raise ValueError("response_capacity_exceeded")
                    buffer += chunk
                    while buffer:
                        end = next((i + 1 for i, c in enumerate(buffer[:240])
                                    if c in "。！？!?\n"), None)
                        if end is None and len(buffer) < 240:
                            break
                        end = end or 240
                        segment, buffer = buffer[:end], buffer[end:]
                        if segment.strip():
                            await speak(segment)
                if buffer.strip():
                    await speak(buffer)
                if self.active == response_id:
                    self.generated = response_id
                    self.emit(Event("generation_completed", response_id))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.active == response_id:
                self.active = None
                self.playback.stop()
                self.emit(Event("response_failed", response_id,
                                "response_timeout" if isinstance(error, TimeoutError)
                                else "provider_failed"))
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    self.emit(Event("provider_cleanup_failed", response_id))

    def acknowledge_playback(self, response_id: str, sequence: int) -> bool:
        """現在responseの実再生ACK。生成完了とは独立して受け付ける。"""
        if self.active != response_id:
            return False
        return self.playback.acknowledge(response_id, sequence)

    def playback_completed(self, response_id: str) -> bool:
        if (self.active != response_id or self.generated != response_id
                or not self.playback.all_confirmed):
            return False
        self.active = None
        self.playback.stop()
        self.emit(Event("playback_completed", response_id))
        return True

    def _cancel_response(self, reason: str) -> None:
        response_id, self.active = self.active, None
        self.generated = None
        self.playback.stop()
        if response_id is not None:
            self.emit(Event("playback_stopped", response_id, reason))
            task = self._responses.get(response_id)
            if task is not None and not task.done():
                task.cancel()
            self.emit(Event("response_cancelled", response_id, reason))

    def _invalidate_input(self) -> None:
        self.generation += 1
        while not self._inputs.empty():
            self._inputs.get_nowait()
            self._inputs.task_done()

    def cancel(self, reason: str = "cancel") -> None:
        self._invalidate_input()
        self._cancel_response(reason)

    def reconnect(self) -> None:
        self.cancel("reconnect")
        self.emit(Event("input_reopen_required"))

    async def drain(self) -> None:
        """fixture用。生成の終了までであり、端末の再生完了ではない。"""
        await self._inputs.join()
        if self._responses:
            await asyncio.gather(*list(self._responses.values()), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        self.cancel("closed")
        tasks = list(self._responses.values())
        if self._input_task is not None:
            self._input_task.cancel()
            tasks.append(self._input_task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1)
            if pending:
                self.emit(Event("shutdown_pending", detail="provider_not_drained"))
