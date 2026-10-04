"""音声sample境界・欠落時の末尾排除・Session分離・入力上限を確認する。"""

from __future__ import annotations
import asyncio
import threading

import numpy as np
import pytest

from local_gpt_live.voice_input.detector import ShortSpeechEvidence, UtteranceDetector
from local_gpt_live.voice_input.models import (
    FRAME_SAMPLES,
    SileroLegacy,
    ShortSpeechAnalyzer,
    check_ready,
)
from local_gpt_live.voice_input.pipeline import (
    AudioInputFault,
    VoiceInputPipeline,
    VoiceInputWorker,
)

VOICE = np.full(FRAME_SAMPLES, 0.03, dtype=np.float32)
QUIET_PCM = bytes(FRAME_SAMPLES * 2)


def test_detected_end_and_last_active_sample_are_distinct() -> None:
    detector = UtteranceDetector()
    events = []
    for i in range(5):
        events.extend(detector.process(VOICE, 0.9, (i + 1) * FRAME_SAMPLES))
    for i in range(5, 13):
        events.extend(
            detector.process(np.zeros_like(VOICE), 0.01, (i + 1) * FRAME_SAMPLES)
        )
    assert [e.kind for e in events] == ["candidate", "confirmed", "ended"]
    assert events[-1].active_end_sample == 5 * FRAME_SAMPLES
    assert events[-1].detected_sample > events[-1].active_end_sample


def test_short_secondary_speech_is_confirmed_at_end() -> None:
    detector = UtteranceDetector()
    events = []
    for i in range(2):
        events.extend(
            detector.process(
                VOICE, 0.1, (i + 1) * FRAME_SAMPLES, ShortSpeechEvidence(1, 0.5, 0.1)
            )
        )
    for i in range(2, 12):
        events.extend(
            detector.process(np.zeros_like(VOICE), 0.01, (i + 1) * FRAME_SAMPLES)
        )
    assert [e.kind for e in events] == ["candidate", "confirmed", "ended"]


def test_invalid_evidence_does_not_confirm_noise() -> None:
    detector = UtteranceDetector()
    observed = []
    for i in range(30):
        observed.extend(
            detector.process(
                VOICE,
                0.01,
                (i + 1) * FRAME_SAMPLES,
                ShortSpeechEvidence(float("nan"), 0, 0),
            )
        )
    assert not any(e.kind == "confirmed" for e in observed)


def test_real_assets_ready_and_model_state_is_session_local() -> None:
    check_ready()
    a, b, reference = SileroLegacy(), SileroLegacy(), SileroLegacy()
    for _ in range(6):
        a.process(VOICE)
        quiet = np.zeros_like(VOICE)
        assert b.process(quiet) == reference.process(quiet)
    a.reset()
    fresh = SileroLegacy()
    assert a.process(VOICE) == fresh.process(VOICE)
    analyzer = ShortSpeechAnalyzer()
    analyzer.close()
    analyzer.close()
    with pytest.raises(RuntimeError, match="vad_closed"):
        analyzer.process(VOICE)


def test_fragmentation_and_wall_clock_do_not_change_sample_position() -> None:
    pipeline = VoiceInputPipeline()
    try:
        assert pipeline.feed(QUIET_PCM[:320], start_sample=0) == ()
        # 到着時刻・待機時間は入力契約に含まれず、sampleだけで進む。
        frames = pipeline.feed(QUIET_PCM[320:], start_sample=160)
        assert len(frames) == 1
        assert frames[0].start_sample == 0
        assert frames[0].end_sample == FRAME_SAMPLES
        assert pipeline.buffered_bytes == 0
    finally:
        pipeline.close()


def test_gap_discards_tail_until_quiet_boundary() -> None:
    pipeline = VoiceInputPipeline()
    loud = (VOICE * 32768).astype("<i2").tobytes()
    try:
        pipeline.feed(loud, start_sample=0)
        with pytest.raises(AudioInputFault, match="audio_gap"):
            pipeline.feed(loud, start_sample=FRAME_SAMPLES + 160)
        next_sample = FRAME_SAMPLES * 2 + 160
        for _ in range(10):
            assert pipeline.feed(loud, start_sample=next_sample) == ()
            next_sample += FRAME_SAMPLES
        for _ in range(8):
            assert pipeline.feed(QUIET_PCM, start_sample=next_sample) == ()
            next_sample += FRAME_SAMPLES
        assert len(pipeline.feed(QUIET_PCM, start_sample=next_sample)) == 1
    finally:
        pipeline.close()


@pytest.mark.parametrize(
    "pcm,start", [(b"x", 0), (b"", 0), (bytes(32002), 0), (bytes(320), -1)]
)
def test_invalid_audio_resets_pending_without_unbounded_buffer(
    pcm: bytes, start: int
) -> None:
    pipeline = VoiceInputPipeline()
    try:
        pipeline.feed(bytes(160), start_sample=0)
        with pytest.raises(AudioInputFault, match="invalid_audio_frame"):
            pipeline.feed(pcm, start_sample=start)
        assert pipeline.buffered_bytes == 0
    finally:
        pipeline.close()


def test_silence_retention_is_bounded_and_close_releases_buffer() -> None:
    pipeline = VoiceInputPipeline()
    for offset in range(0, 16000 * 20, 160):
        pipeline.feed(bytes(320), start_sample=offset)
        assert pipeline.buffered_bytes < FRAME_SAMPLES * 2
    pipeline.close()
    assert pipeline.buffered_bytes == 0
    with pytest.raises(RuntimeError, match="vad_closed"):
        pipeline.feed(QUIET_PCM, start_sample=0)


def test_worker_drops_results_from_reset_epoch() -> None:
    async def scenario() -> None:
        pipeline = VoiceInputPipeline()
        worker = VoiceInputWorker(pipeline)
        started, release = threading.Event(), threading.Event()
        original = pipeline.feed

        def slow(pcm: bytes, *, start_sample: int):
            started.set()
            release.wait(timeout=5)
            return original(pcm, start_sample=start_sample)

        pipeline.feed = slow
        active = asyncio.create_task(worker.process(QUIET_PCM, start_sample=0))
        await asyncio.to_thread(started.wait, 5)
        reset = asyncio.create_task(worker.reset(0))
        await asyncio.sleep(0)
        release.set()
        assert await active == ()
        await reset
        await worker.close()

    asyncio.run(scenario())


def test_long_confirmed_utterance_discards_remaining_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = VoiceInputPipeline()
    monkeypatch.setattr(pipeline._model, "process", lambda frame: 0.9)
    loud = (VOICE * 32768).astype("<i2").tobytes()
    try:
        next_sample = 0
        with pytest.raises(AudioInputFault, match="input_capacity_exceeded"):
            for _ in range(400):
                pipeline.feed(loud, start_sample=next_sample)
                next_sample += FRAME_SAMPLES
        assert pipeline.buffered_bytes == 0
        next_sample += FRAME_SAMPLES
        assert pipeline.feed(loud, start_sample=next_sample) == ()
        for _ in range(8):
            next_sample += FRAME_SAMPLES
            assert pipeline.feed(QUIET_PCM, start_sample=next_sample) == ()
    finally:
        pipeline.close()


def test_asset_mismatch_is_not_ready(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from local_gpt_live.voice_input import models

    monkeypatch.setattr(models, "ASSETS", tmp_path)
    (tmp_path / "libfvad.wasm").write_bytes(b"invalid model")
    with pytest.raises(models.VadPreparationError, match="vad_asset_mismatch"):
        models._verified_asset("libfvad.wasm", models.FVAD_SHA256)
    with pytest.raises(models.VadPreparationError, match="vad_asset_unavailable"):
        models._verified_asset("missing", models.MODEL_SHA256)


def test_worker_backlog_counts_audio_samples_and_discards_old_queue() -> None:
    async def scenario() -> None:
        pipeline = VoiceInputPipeline()
        worker = VoiceInputWorker(pipeline)
        started, release = threading.Event(), threading.Event()
        original = pipeline.feed
        calls = []

        def slow(pcm: bytes, *, start_sample: int):
            calls.append(start_sample)
            started.set()
            assert release.wait(timeout=5)
            return original(pcm, start_sample=start_sample)

        pipeline.feed = slow
        first = asyncio.create_task(worker.process(bytes(32000), start_sample=0))
        assert await asyncio.to_thread(started.wait, 5)
        second = asyncio.create_task(worker.process(bytes(16000), start_sample=16000))
        await asyncio.sleep(0)
        try:
            with pytest.raises(AudioInputFault, match="vad_backlog_exceeded"):
                await worker.process(QUIET_PCM, start_sample=24000)
            with pytest.raises(AudioInputFault, match="vad_reset_required"):
                await worker.process(QUIET_PCM, start_sample=24000)
            release.set()
            assert await first == ()
            assert await second == ()
            assert calls == [0]
            await worker.reset(30000, quarantine=True)
            assert await worker.process(QUIET_PCM, start_sample=30000) == ()
        finally:
            release.set()
            await worker.close()

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_release_executing_audio() -> None:
    async def scenario() -> None:
        pipeline = VoiceInputPipeline()
        worker = VoiceInputWorker(pipeline)
        started, release = threading.Event(), threading.Event()
        original = pipeline.feed

        def slow(pcm: bytes, *, start_sample: int):
            started.set()
            assert release.wait(timeout=5)
            return original(pcm, start_sample=start_sample)

        pipeline.feed = slow
        active = asyncio.create_task(worker.process(QUIET_PCM, start_sample=0))
        assert await asyncio.to_thread(started.wait, 5)
        try:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            assert worker._pending_samples == FRAME_SAMPLES
            with pytest.raises(AudioInputFault, match="vad_reset_required"):
                await worker.process(QUIET_PCM, start_sample=FRAME_SAMPLES)
            reset = asyncio.create_task(worker.reset(0))
            await asyncio.sleep(0)
            assert not reset.done()
            release.set()
            await reset
            assert worker._pending_samples == 0
            assert len(await worker.process(QUIET_PCM, start_sample=0)) == 1
        finally:
            release.set()
            await worker.close()

    asyncio.run(scenario())
