import numpy as np
import pytest

from local_gpt_live.demo import FixtureCore, FixtureStt, FixtureTts
from local_gpt_live.input import AudioInput
from local_gpt_live.playback import Playback
from local_gpt_live.session import VoiceSession
from local_gpt_live.voice_input.detector import Detection, ShortSpeechEvidence
from local_gpt_live.voice_input.pipeline import ProcessedFrame, VoiceInputPipeline
from local_gpt_live.voice_input.session import SpeechBoundary


async def test_track_replacement_clears_preroll_without_active_utterance():
    session = VoiceSession(FixtureStt(), FixtureCore(), FixtureTts(), Playback())
    ingress = AudioInput(session, VoiceInputPipeline())
    try:
        old = await ingress.open(track_sid="old", request_id="old", revision=1)
        # 有声候補前のPCMも次trackへ持ち越さない。
        ingress._frame(ProcessedFrame(0, 1536, b"\x01\x00" * 1536, 0,
                                       ShortSpeechEvidence(0, 1, 1), (), False))
        new = await ingress.open(track_sid="new", request_id="new", revision=2)
        with pytest.raises(ValueError, match="stale_input_request"):
            ingress.reconnect(revision=1)
        assert ingress.backend.grant is new
        assert ingress._preroll == b""
        await ingress.backend.receive(bytes(3072), start_sample=0, grant=old)
        assert ingress._preroll == b""
        await ingress.backend.receive(bytes(3072), start_sample=0, grant=new)
        assert len(ingress._preroll) == 3072
    finally:
        await ingress.close()


async def test_formal_pcm_capture_reaches_stt_and_reconnect_discards_open_capture():
    class Stt:
        inputs = []

        async def transcribe(self, pcm):
            self.inputs.append(pcm)
            return "fixture"

    stt = Stt()
    session = VoiceSession(stt, FixtureCore(), FixtureTts(), Playback())
    ingress = AudioInput(session, VoiceInputPipeline())
    try:
        grant = await ingress.open(track_sid="old", request_id="old", revision=1)
        pcm = np.full(1536, 1000, dtype="<i2").tobytes()
        boundary = SpeechBoundary("utterance", grant, Detection("confirmed", 0, 1536, 1536))
        ingress._frame(ProcessedFrame(0, 1536, pcm, .9, ShortSpeechEvidence(1, .5, .1), (), False))
        ingress._started(boundary)
        await ingress._stopped(boundary)
        await session.drain()
        assert stt.inputs == [pcm]
        ingress._started(boundary)
        ingress.reconnect(revision=2)
        await ingress._stopped(boundary)
        await session.drain()
        assert stt.inputs == [pcm]
    finally:
        await ingress.close()
