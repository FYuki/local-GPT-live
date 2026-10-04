"""端末に渡す直前にもresponseを照合する、容量制限付き出力境界。"""

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioPacket:
    response_id: str
    sequence: int
    wav: bytes


class Playback:
    """実端末adapterはstopでデバイス内queueも同期的に破棄すること。"""

    def __init__(self, max_bytes: int = 4_000_000) -> None:
        if max_bytes <= 0:
            raise ValueError("invalid_playback_capacity")
        self.max_bytes = max_bytes
        self.active: str | None = None
        self._packets: deque[AudioPacket] = deque()
        self._bytes = 0
        self._next_sequence = 0

    def start(self, response_id: str) -> None:
        self.stop()
        self.active = response_id

    def stop(self) -> None:
        self.active = None
        self._packets.clear()
        self._bytes = 0
        self._next_sequence = 0

    def enqueue(self, packet: AudioPacket) -> bool:
        if packet.response_id != self.active:
            return False
        if packet.sequence != self._next_sequence:
            raise ValueError("audio_sequence_invalid")
        if not packet.wav or self._bytes + len(packet.wav) > self.max_bytes:
            raise ValueError("playback_capacity_exceeded")
        self._packets.append(packet)
        self._bytes += len(packet.wav)
        self._next_sequence += 1
        return True

    def consume(self) -> AudioPacket | None:
        """配信済みは実再生済みではない。端末は別途完了ACKを返す。"""
        if not self._packets:
            return None
        packet = self._packets.popleft()
        self._bytes -= len(packet.wav)
        return packet if packet.response_id == self.active else None

    @property
    def pending_bytes(self) -> int:
        return self._bytes
