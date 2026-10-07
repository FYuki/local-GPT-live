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
        self._delivered_sequence = -1
        self._confirmed_sequence = -1

    def start(self, response_id: str) -> None:
        self.stop()
        self.active = response_id

    def stop(self) -> None:
        self.active = None
        self._packets.clear()
        self._bytes = 0
        self._next_sequence = 0
        self._delivered_sequence = -1
        self._confirmed_sequence = -1

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

    def generation_completed(self, response_id: str) -> None:
        """出力adapterが生成終端を観測するためのhook。基底はACK待ちを維持する。"""

    @property
    def last_audio_sequence(self) -> int:
        """現在responseへenqueueした最後の番号。音声なしでは-1。"""
        return self._next_sequence - 1

    def consume(self) -> AudioPacket | None:
        """配信済みは実再生済みではない。端末は別途完了ACKを返す。"""
        if not self._packets:
            return None
        packet = self._packets.popleft()
        self._bytes -= len(packet.wav)
        if packet.response_id != self.active:
            return None
        self._delivered_sequence = packet.sequence
        return packet

    def acknowledge(self, response_id: str, sequence: int) -> bool:
        """認証済みtransportから、区間全体の実再生ACKを順に受け取る。

        配信済み区間だけを受理する。再送は冪等、飛び越しは拒否する。
        認証・端末の出力時計との照合は呼び出すadapterの責務。
        """
        if (response_id != self.active or type(sequence) is not int or sequence < 0
                or sequence > self._delivered_sequence):
            return False
        if sequence <= self._confirmed_sequence:
            return True
        if sequence != self._confirmed_sequence + 1:
            return False
        self._confirmed_sequence = sequence
        return True

    @property
    def confirmed_sequence(self) -> int:
        """現在responseの、全体再生がACKされた連続prefixの末尾。"""
        return self._confirmed_sequence

    @property
    def all_confirmed(self) -> bool:
        return (self.active is not None and not self._packets
                and self._confirmed_sequence == self._next_sequence - 1)

    @property
    def pending_bytes(self) -> int:
        return self._bytes
