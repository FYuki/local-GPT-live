import asyncio

from local_gpt_live.demo import FixtureCore, FixtureStt, FixtureTts
from local_gpt_live.input import AudioInput
from local_gpt_live.playback import Playback
from local_gpt_live.session import VoiceSession
from local_gpt_live.voice_input.detector import Detection, ShortSpeechEvidence
from local_gpt_live.voice_input.pipeline import ProcessedFrame, VoiceInputPipeline
from local_gpt_live.voice_input.session import SpeechBoundary


async def test_preview_stops_old_answer_before_final_input_and_rejects_stale():
    stt = FixtureStt()
    session = VoiceSession(stt, FixtureCore(), FixtureTts(), Playback())
    pcm = b"aa"
    stt.transcripts[pcm] = "はい、止めて"
    try:
        old = session.submit_text("説明")
        await session.drain()
        assert not await session.preview(pcm, generation=session.generation, overlap=old,
                                         is_current=lambda: False)
        assert session.active == old
        assert await session.preview(pcm, generation=session.generation, overlap=old,
                                     is_current=lambda: True)
        assert session.active is None and session.playback.pending_bytes == 0
        session.submit_audio(pcm, generation=session.generation, overlap=old)
        await session.drain()
        assert session.active is not None and session.active != old
    finally:
        await session.close()


async def test_preview_800ms_excludes_quiet_preroll_and_limits_three_attempts():
    class Stt:
        calls = 0

        async def transcribe(self, pcm):
            self.calls += 1
            return "うん"

    stt = Stt()
    session = VoiceSession(stt, FixtureCore(), FixtureTts(), Playback())
    ingress = AudioInput(session, VoiceInputPipeline())
    try:
        grant = await ingress.open(track_sid="fixture", request_id="open", revision=1)
        old = session.submit_text("説明")
        await session.drain()
        quiet = ProcessedFrame(0, 32000, bytes(64000), 0, ShortSpeechEvidence(0, 1, 1), (), False)
        ingress._frame(quiet)
        boundary = SpeechBoundary("utterance", grant, Detection("confirmed", 0, 1, 1))
        ingress._started(boundary)
        assert ingress._preview_task is None
        speech = ProcessedFrame(32000, 44800, b"\x01\x04" * 12800, .9,
                                ShortSpeechEvidence(1, .5, .1), (), False)
        for _ in range(5):
            ingress._frame(speech)
            if ingress._preview_task is not None:
                await ingress._preview_task
        assert stt.calls == 3 and session.active == old
    finally:
        await ingress.close()


async def test_timeout_invalidates_tts_even_if_provider_swallows_cancellation():
    cancelled, release = asyncio.Event(), asyncio.Event()

    class Tts(FixtureTts):
        async def synthesize(self, text):
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
            return await super().synthesize(text)

    events = []
    session = VoiceSession(FixtureStt(), FixtureCore(), Tts(), Playback(),
                           response_timeout=.01, emit=events.append)
    try:
        session.submit_text("fixture")
        await asyncio.wait_for(cancelled.wait(), 1)
        assert session.active is None
        release.set()
        await session.drain()
        assert session.playback.consume() is None
        assert any(e.kind == "response_failed" and e.detail == "response_timeout" for e in events)
    finally:
        release.set()
        await session.close()
