"""既存UtteranceDetectorを、音声sample時計で動くBE処理へ移す。"""

from __future__ import annotations
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Literal
import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class ShortSpeechEvidence:
    voiced_fraction: float
    tonal_concentration: float
    spectral_flatness: float

    @property
    def valid(self) -> bool:
        return all(
            isfinite(v) and 0 <= v <= 1
            for v in (
                self.voiced_fraction,
                self.tonal_concentration,
                self.spectral_flatness,
            )
        )


@dataclass(frozen=True)
class DetectorOptions:
    sample_rate: int = 16000
    minimum_rms: float = 0.001
    minimum_active_ms: float = 100
    silence_ms: float = 600
    strong_probability: float = 0.3
    minimum_strong_ms: float = 300
    short_strong_ms: float = 250
    high_probability: float = 0.4
    minimum_high_ms: float = 192
    evidence_probability: float = 0.2
    minimum_evidence_ms: float = 140
    negative_probability: float = 0.25
    neural_silence_ms: float = 700
    evidence_window_ms: float = 2000


@dataclass(frozen=True)
class Detection:
    kind: Literal["candidate", "confirmed", "ended", "misfire"]
    started_sample: int
    detected_sample: int
    # 活動の末尾と、無音待機を終えて通知した位置を分離する。
    active_end_sample: int


class UtteranceDetector:
    def __init__(self, options: DetectorOptions = DetectorOptions()) -> None:
        self.options = options
        self.reset()

    def reset(self) -> None:
        self._pending_voice: list[tuple[float, float]] = []
        self._short_speech: list[tuple[float, float]] = []
        self._candidate: float | None = None
        self._last_active = 0.0
        self._active: list[tuple[float, float]] = []
        self._evidence: list[tuple[float, float, float]] = []
        self._strong: list[tuple[float, float]] = []
        self._neural_quiet = 0.0
        self._confirmed = False
        self._consecutive_high = 0.0

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    @property
    def has_candidate(self) -> bool:
        return self._candidate is not None

    def _event(
        self, kind: Literal["candidate", "confirmed", "ended", "misfire"], end: int
    ) -> Detection:
        assert self._candidate is not None
        rate = self.options.sample_rate / 1000
        return Detection(
            kind, round(self._candidate * rate), end, round(self._last_active * rate)
        )

    def process(
        self,
        frame: NDArray[np.float32],
        probability: float,
        end_sample: int,
        secondary: ShortSpeechEvidence | None = None,
    ) -> tuple[Detection, ...]:
        o = self.options
        if (
            frame.ndim != 1
            or frame.size == 0
            or not np.isfinite(frame).all()
            or not isfinite(probability)
            or not 0 <= probability <= 1
            or type(end_sample) is not int
            or end_sample < frame.size
        ):
            return ()
        end = end_sample * 1000 / o.sample_rate
        duration = frame.size * 1000 / o.sample_rate
        # JSのNumberと同じfloat64で積算する。
        active = (
            sqrt(float(np.sum(frame.astype(np.float64) ** 2)) / frame.size)
            >= o.minimum_rms
        )
        window_start = end - o.evidence_window_ms
        events: list[Detection] = []
        if not self._confirmed:
            self._active = [v for v in self._active if v[1] > window_start]
        if active:
            if self._candidate is None:
                self._candidate = max(0, end - duration)
                events.append(self._event("candidate", end_sample))
            self._last_active = end
            if not self._confirmed:
                self._active.append((max(0, end - duration), end))
        if self._candidate is None:
            return tuple(events)
        self._short_speech = [v for v in self._short_speech if v[0] > window_start]
        self._pending_voice = [v for v in self._pending_voice if v[0] > window_start]
        secondary_voiced = bool(
            active
            and secondary is not None
            and secondary.valid
            and secondary.voiced_fraction >= 0.6
            and secondary.spectral_flatness < 0.3
        )
        if not self._confirmed and secondary_voiced and secondary is not None:
            self._pending_voice.append((end, duration * secondary.voiced_fraction))
        secondary_speech = (
            secondary_voiced
            and secondary is not None
            and secondary.tonal_concentration < 0.9
        )
        if secondary_speech and secondary is not None:
            self._short_speech.append((end, duration * secondary.voiced_fraction))
        if not self._confirmed and self._active:
            self._candidate = max(self._active[0][0], window_start)
        self._strong = [v for v in self._strong if v[0] > window_start]
        if probability >= o.strong_probability:
            self._strong.append((end, duration))
            self._neural_quiet = 0
        elif self._confirmed and secondary_speech:
            self._neural_quiet = 0
        elif probability < o.negative_probability:
            self._neural_quiet += duration
        self._consecutive_high = (
            min(self._consecutive_high + duration, o.minimum_high_ms)
            if active and probability >= o.high_probability
            else 0
        )
        self._evidence = [v for v in self._evidence if v[0] > window_start]
        if not self._confirmed and active and probability >= o.evidence_probability:
            self._evidence.append((end, duration, probability))
        evidence_ms = sum(p * min(d, e - window_start) for e, d, p in self._evidence)
        strong_ms = sum(min(d, e - window_start) for e, d in self._strong)
        active_ms = sum(e - max(s, window_start) for s, e in self._active)
        short_ms = sum(min(d, e - window_start) for e, d in self._short_speech)
        short_fallback = (
            end - self._last_active > o.silence_ms
            and self._last_active - self._candidate <= 1000
            and short_ms >= 160
        )
        if (
            not self._confirmed
            and active_ms >= o.minimum_active_ms
            and (
                short_fallback
                or strong_ms >= o.minimum_strong_ms
                or (
                    (
                        strong_ms >= o.short_strong_ms
                        or evidence_ms >= o.minimum_evidence_ms
                    )
                    and self._consecutive_high >= o.minimum_high_ms
                )
            )
        ):
            self._confirmed = True
            self._active = []
            events.append(self._event("confirmed", end_sample))
        pending_ms = sum(min(d, e - window_start) for e, d in self._pending_voice)
        pending = (
            not self._confirmed
            and active_ms >= o.minimum_active_ms
            and self._last_active - self._candidate <= 1000
            and pending_ms >= 160
            and end - self._candidate < o.evidence_window_ms
        )
        ended = (end - self._last_active > o.silence_ms and not pending) or (
            self._confirmed and self._neural_quiet > o.neural_silence_ms
        )
        if ended:
            events.append(
                self._event("ended" if self._confirmed else "misfire", end_sample)
            )
            self.reset()
        return tuple(events)
