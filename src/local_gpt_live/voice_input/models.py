"""同一Silero legacy・libfvad資産をCPUで実行する。音声・本文を永続化しない。"""

from __future__ import annotations
import hashlib
from functools import lru_cache
from pathlib import Path
import numpy as np
from numpy.typing import NDArray
import onnxruntime as ort  # type: ignore[import-untyped]
import wasmtime
from local_gpt_live.voice_input.detector import ShortSpeechEvidence

ASSETS = Path(__file__).with_name("assets")
MODEL_SHA256 = "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28"
FVAD_SHA256 = "3fadafc9c5c1c3117d0178ae631484c6dabc72e0f8742b95c370cd39f46cf171"
FRAME_SAMPLES = 1536
SAMPLE_RATE = 16000


class VadPreparationError(RuntimeError):
    """詳細なpath・例外本文はwireへ出さず、利用不可として扱う。"""


def _verified_asset(name: str, expected: str) -> bytes:
    try:
        data = (ASSETS / name).read_bytes()
    except OSError as error:
        raise VadPreparationError("vad_asset_unavailable") from error
    if hashlib.sha256(data).hexdigest() != expected:
        raise VadPreparationError("vad_asset_mismatch")
    return data


@lru_cache(maxsize=1)
def _model_session() -> ort.InferenceSession:
    try:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            _verified_asset("silero_vad_legacy.onnx", MODEL_SHA256),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        if {x.name for x in session.get_inputs()} != {"input", "h", "c", "sr"}:
            raise VadPreparationError("vad_model_contract_invalid")
        if {x.name for x in session.get_outputs()} != {"output", "hn", "cn"}:
            raise VadPreparationError("vad_model_contract_invalid")
        return session
    except VadPreparationError:
        raise
    except Exception as error:
        raise VadPreparationError("vad_model_initialization_failed") from error


@lru_cache(maxsize=1)
def _fvad_module() -> tuple[wasmtime.Engine, wasmtime.Module]:
    try:
        engine = wasmtime.Engine()
        module = wasmtime.Module(engine, _verified_asset("libfvad.wasm", FVAD_SHA256))
        if module.imports:
            raise VadPreparationError("vad_wasm_contract_invalid")
        return engine, module
    except VadPreparationError:
        raise
    except Exception as error:
        raise VadPreparationError("vad_wasm_initialization_failed") from error


def _valid_frame(frame: NDArray[np.float32]) -> None:
    if (
        frame.dtype != np.float32
        or frame.shape != (FRAME_SAMPLES,)
        or not np.isfinite(frame).all()
    ):
        raise ValueError("invalid_vad_frame")


class SileroLegacy:
    def __init__(self) -> None:
        self._session = _model_session()
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def process(self, frame: NDArray[np.float32]) -> float:
        _valid_frame(frame)
        output, h, c = self._session.run(
            ["output", "hn", "cn"],
            {
                "input": frame.reshape(1, FRAME_SAMPLES),
                "h": self._h,
                "c": self._c,
                "sr": np.array(16000, dtype=np.int64),
            },
        )
        probability = float(output[0, 0])
        if not np.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("invalid_vad_probability")
        self._h, self._c = h, c
        return probability


def spectral_evidence(frame: NDArray[np.float32]) -> tuple[float, float]:
    """既存JSの1024点Hann・正周波数512bin・上位2帯域を保つ。"""
    _valid_frame(frame)
    power = (
        np.abs(np.fft.rfft(frame[-1024:].astype(np.float64) * np.hanning(1024))[1:])
        ** 2
    )
    total = float(np.sum(power))
    if total == 0:
        return 1.0, 1.0
    flatness = float(
        np.exp(np.mean(np.log(np.maximum(power, total * 1e-12)))) / (total / 512)
    )
    remaining = power.copy()
    concentration = 0.0
    for _ in range(2):
        bands = np.convolve(remaining, np.ones(5), mode="same")
        center = int(np.argmax(bands))
        concentration += float(bands[center])
        remaining[max(0, center - 2) : min(512, center + 3)] = 0
    return min(1.0, concentration / total), min(1.0, flatness)


class ShortSpeechAnalyzer:
    def __init__(self) -> None:
        engine, module = _fvad_module()
        self._store = wasmtime.Store(engine)
        self._store.set_limits(memory_size=16 * 1024 * 1024, instances=1, memories=1)
        instance = wasmtime.Instance(self._store, module, [])
        self._exports = instance.exports(self._store)
        self._pointer = self._function("malloc")(self._store, 320)
        self._handle = 0
        self._closed = False
        self._pending: NDArray[np.float32] = np.empty(0, dtype=np.float32)
        if not self._pointer:
            raise VadPreparationError("vad_wasm_allocation_failed")
        try:
            self.reset()
        except Exception:
            self.close()
            raise

    def _function(self, name: str) -> wasmtime.Func:
        value = self._exports[name]
        if not isinstance(value, wasmtime.Func):
            raise VadPreparationError("vad_wasm_contract_invalid")
        return value

    def _memory(self) -> wasmtime.Memory:
        value = self._exports["memory"]
        if not isinstance(value, wasmtime.Memory):
            raise VadPreparationError("vad_wasm_contract_invalid")
        return value

    def reset(self) -> None:
        if self._closed:
            raise RuntimeError("vad_closed")
        if self._handle:
            self._function("fvad_free")(self._store, self._handle)
        self._handle = self._function("fvad_new")(self._store)
        self._pending = np.empty(0, dtype=np.float32)
        if (
            not self._handle
            or self._function("fvad_set_mode")(self._store, self._handle, 0) != 0
            or self._function("fvad_set_sample_rate")(
                self._store, self._handle, SAMPLE_RATE
            )
            != 0
        ):
            raise VadPreparationError("vad_wasm_initialization_failed")

    def process(self, frame: NDArray[np.float32]) -> ShortSpeechEvidence:
        if self._closed:
            raise RuntimeError("vad_closed")
        _valid_frame(frame)
        combined = np.concatenate((self._pending, frame))
        voiced = count = offset = 0
        while offset + 160 <= combined.size:
            floats = np.clip(combined[offset : offset + 160].astype(np.float64), -1, 1)
            samples = np.trunc(
                np.where(floats < 0, floats * 32768, floats * 32767)
            ).astype("<i2")
            self._memory().write(self._store, samples.tobytes(), self._pointer)
            result = self._function("fvad_process")(
                self._store, self._handle, self._pointer, 160
            )
            if result not in (0, 1):
                raise ValueError("vad_wasm_processing_failed")
            count += 1
            voiced += result
            offset += 160
        self._pending = combined[offset:].copy()
        concentration, flatness = spectral_evidence(frame)
        return ShortSpeechEvidence(voiced / count, concentration, flatness)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle:
            self._function("fvad_free")(self._store, self._handle)
            self._handle = 0
        if self._pointer:
            self._function("free")(self._store, self._pointer)
            self._pointer = 0
        self._pending = np.empty(0, dtype=np.float32)
        self._store.close()


def check_ready() -> None:
    """CPU推論・補助VADを実行できてから準備完了とする。"""
    try:
        frame = np.zeros(FRAME_SAMPLES, dtype=np.float32)
        SileroLegacy().process(frame)
        analyzer = ShortSpeechAnalyzer()
        try:
            analyzer.process(frame)
        finally:
            analyzer.close()
    except VadPreparationError:
        raise
    except Exception as error:
        raise VadPreparationError("vad_initialization_failed") from error
