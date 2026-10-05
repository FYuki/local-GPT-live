"""接続も資格情報発行も行わない、スモーク補助コードの合成検査。"""

import asyncio
import base64
import contextlib
import io
import importlib.util
import sys
import json
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "livekit_smoke", Path(__file__).resolve().parents[1] / "tools/livekit_smoke.py",
)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)


def fixture_token(identity="backend", room="reserved-test", **changes):
    payload = {"sub": identity, "exp": time.time() + 90,
               "video": {"room": room, "roomJoin": True, "canPublish": True,
                         "canSubscribe": True, "canPublishSources": ["microphone"]}}
    payload.update(changes)
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    # 実サーバーが受理しない、署名を持たない fixture。
    return "fixture." + encoded + ".not-a-signature"


class OfflineTests(unittest.TestCase):
    def test_compile(self):
        path = Path(smoke.__file__)
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_loopback_only(self):
        for url in ("ws://127.0.0.1:7880", "wss://localhost", "ws://[::1]:7880/"):
            self.assertEqual(smoke.validate_url(url), url)
        for url in ("", "https://localhost", "ws://example.com", "ws://127.0.0.1.evil",
                    "ws://user:pass@localhost", "ws://localhost?token=fixture",
                    "ws://localhost/path", "ws://localhost:99999"):
            with self.subTest(url=url), self.assertRaises(smoke.SmokeFailure):
                smoke.validate_url(url)

    def test_exact_room_and_distinct_identities(self):
        environment = {"LIVEKIT_URL": "ws://127.0.0.1:7880",
                       "LIVEKIT_BACKEND_TOKEN": fixture_token(),
                       "LIVEKIT_SYNTHETIC_TOKEN": fixture_token("synthetic")}
        config = smoke.configuration(environment, "reserved-test")
        self.assertEqual(config.synthetic.identity, "synthetic")
        self.assertNotIn(environment["LIVEKIT_BACKEND_TOKEN"], repr(config))
        for requested in (None, "other-room"):
            with self.assertRaises(smoke.SmokeFailure):
                smoke.configuration(environment, requested)
        environment["LIVEKIT_SYNTHETIC_TOKEN"] = fixture_token()
        with self.assertRaises(smoke.SmokeFailure):
            smoke.configuration(environment, "reserved-test")

    def test_invalid_grants_and_expiry(self):
        invalid = ["", "not-jwt", fixture_token(exp=time.time() + 30),
                   fixture_token(nbf=time.time() + 60), fixture_token(video={}),
                   fixture_token(sub="bad\nidentity")]
        for token in invalid:
            with self.subTest(), self.assertRaises(smoke.SmokeFailure):
                smoke.inspect_token(token)

    def test_no_run_does_not_call_socket_or_runtime(self):
        with patch.dict(smoke.os.environ, {}, clear=True), patch("socket.socket") as socket:
            with patch.object(smoke, "bounded_run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(smoke.main([]), 0)
            socket.assert_not_called()
            run.assert_not_called()
            self.assertFalse(json.loads(out.getvalue())["connection_attempted"])

    def test_run_without_configuration_cannot_connect(self):
        with patch.dict(smoke.os.environ, {}, clear=True), patch.object(smoke, "bounded_run") as run:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(smoke.main(["--run", "--room", "reserved-test"]), 1)
            run.assert_not_called()

    def test_pcm_wav_and_probe(self):
        with wave.open(io.BytesIO(smoke.synthetic_wav(3200))) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(),
                              wav.getnframes()), (16000, 1, 2, 3200))
        probe = smoke.ProbePipeline()
        for start in range(0, 1600, 160):
            self.assertEqual(probe.feed(smoke.pcm(160, tone=True), start_sample=start), ())
        self.assertEqual(probe.samples, 1600)
        self.assertGreater(probe.nonzero, 0)
        with self.assertRaises(smoke.SmokeFailure):
            probe.feed(smoke.pcm(160, tone=False), start_sample=0)
        probe.close()
        self.assertTrue(probe.closed)


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_reader_exception_cannot_pass_after_it_was_observed(self):
        async def failed_reader():
            raise RuntimeError("synthetic reader failure")

        reader = asyncio.create_task(failed_reader())
        await asyncio.gather(reader, return_exceptions=True)
        self.assertIsInstance(reader.exception(), RuntimeError)
        with self.assertRaisesRegex(smoke.SmokeFailure, "^output_reader_failed$"):
            smoke.check_readers([reader])

    async def test_reader_completion_requires_success(self):
        successful = asyncio.create_task(asyncio.sleep(0))
        await successful
        smoke.check_readers([successful])
        pending = asyncio.create_task(asyncio.Event().wait())
        try:
            with self.assertRaisesRegex(smoke.SmokeFailure, "^output_reader_not_closed$"):
                smoke.check_readers([successful, pending])
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        with self.assertRaisesRegex(smoke.SmokeFailure, "^output_reader_failed$"):
            smoke.check_readers([pending])

    async def test_fake_providers_use_real_session_and_cancel(self):
        from local_gpt_live.input import AudioInput
        from local_gpt_live.playback import Playback
        from local_gpt_live.session import VoiceSession

        providers, probe, events = smoke.Providers(), smoke.ProbePipeline(), []
        session = VoiceSession(providers, providers, providers, Playback(), emit=events.append)
        audio = AudioInput(session, probe)
        try:
            response = audio.submit_text("short", revision=1)
            await session.drain()
            packets = [session.playback.consume(), session.playback.consume()]
            self.assertEqual([(packet.response_id, packet.sequence) for packet in packets],
                             [(response, 0), (response, 1)])
            audio.submit_text("long", revision=2)
            await smoke.wait_until(lambda: session.playback.pending_bytes > 0, "fixture_timeout")
            session.cancel()
            await asyncio.wait_for(session.drain(), 1)
            self.assertTrue(providers.long_cancelled)
            self.assertEqual(providers.stt_calls, 0)
            self.assertFalse(any(event.kind == "playback_completed" for event in events))
        finally:
            await audio.close()
        self.assertTrue(probe.closed)


if __name__ == "__main__":
    unittest.main()
