"""新しいローカル起動入口の組込み。共有サービス・実マイクは使わない。"""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import asyncio
import io
import wave

import httpx
import pytest

from local_gpt_live.livekit_host import LiveKitHost
from local_gpt_live.livekit_transport import LiveKitPlayback
from local_gpt_live.providers import CoreChat, Voicevox, Whisper
from test_livekit_host import HostRig, Stream
from test_livekit_transport import eventually


def test_local_browser_host_command_has_a_reachable_help_entry(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    directory = Path(__file__).resolve().parents[1] / ".takt" / "browser-demo-fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="help-", dir=directory) as cwd:
        result = subprocess.run([sys.executable, "-m", "local_gpt_live.browser_demo", "--help"],
                                capture_output=True, text=True, env=env, cwd=cwd, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


async def test_conversation_factory_uses_explicit_existing_providers_and_owns_cleanup():
    module = importlib.import_module("local_gpt_live.browser_demo")
    app = module.create_demo(
        url="ws://127.0.0.1:7880", token="synthetic-test-token",
        participant_identity="fixture-user", participant_sid="PA-fixture",
        session_id="session-fixture", mode="conversation",
        stt_url="http://127.0.0.1:18101", core_url="http://127.0.0.1:18102",
        tts_url="http://127.0.0.1:18103", core_alias="fixture-alias", speaker_id=3,
    )
    host = app.host
    assert isinstance(host, LiveKitHost)
    try:
        assert host.transport.config.url == "ws://127.0.0.1:7880"
        assert host.transport.config.token == "synthetic-test-token"
        assert host.transport.config.participant_identity == "fixture-user"
        assert host.transport.config.participant_sid == "PA-fixture"
        assert host.session_id == "session-fixture"
        assert isinstance(host.session.playback, LiveKitPlayback)
        assert isinstance(host.session.stt, Whisper)
        assert isinstance(host.session.core, CoreChat)
        assert isinstance(host.session.tts, Voicevox)
        assert str(host.session.stt.http.base_url).rstrip("/") == "http://127.0.0.1:18101"
        assert str(host.session.core.http.base_url).rstrip("/") == "http://127.0.0.1:18102"
        assert str(host.session.tts.http.base_url).rstrip("/") == "http://127.0.0.1:18103"
        assert host.session.core.model == "fixture-alias"
        assert host.session.tts.speaker_id == 3
    finally:
        await app.aclose()
    assert host.session.stt.http.is_closed
    assert host.session.core.http.is_closed
    assert host.session.tts.http.is_closed


@pytest.mark.parametrize("missing", ["stt_url", "core_url", "tts_url", "core_alias"])
def test_conversation_mode_does_not_fall_back_to_diagnostics(missing):
    module = importlib.import_module("local_gpt_live.browser_demo")
    settings = dict(url="ws://127.0.0.1:7880", token="synthetic-test-token",
                    participant_identity="fixture-user", participant_sid="PA-fixture",
                    session_id="session-fixture", mode="conversation",
                    stt_url="http://127.0.0.1:18101", core_url="http://127.0.0.1:18102",
                    tts_url="http://127.0.0.1:18103", core_alias="fixture-alias", speaker_id=3)
    settings[missing] = ""
    with pytest.raises(Exception):
        module.create_demo(**settings)


async def test_explicit_diagnostics_uses_real_host_ready_gate_without_shared_inference(monkeypatch):
    module = importlib.import_module("local_gpt_live.browser_demo")
    calls = []

    async def forbidden_send(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("診断で共有推論を呼ばない")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)
    fixture = HostRig(monkeypatch)
    app = module.create_demo(
        url="ws://127.0.0.1:7880", token="synthetic-test-token",
        participant_identity="fixture-user", participant_sid="PA-fixture",
        session_id="session-fixture", mode="diagnostics",
        stt_url=None, core_url=None, tts_url=None, core_alias=None, speaker_id=None,
    )
    host = app.host
    fixture.host = host
    try:
        assert app.mode == "diagnostics"
        await host.connect()
        fixture.control_method = "local-gpt-live.control.v1"
        fixture.ack_method = "local-gpt-live.playback-ack.v1"
        await host._notifications.join()
        result = await fixture.control("text", text="合成診断要求")
        assert result["ok"]
        await eventually(lambda: bool(fixture.participant.publications))
        await host._notifications.join()
        metadata = next(n[0] for n in fixture.participant.notifications
                        if n[0]["type"] == "output_track")
        assert sum(source.capture_calls for source in fixture.rig.sources) == 0
        assert (await fixture.ready(result["response_id"], metadata["track_sid"]))["ok"]
        await eventually(lambda: sum(source.capture_calls for source in fixture.rig.sources) > 0)
        assert app.diagnostics.snapshot()["input_samples"] == 0
        prepared = await fixture.control("open_input", track_sid="TR-input",
                                         expected_revision=host.transport.audio.backend.revision)
        assert prepared["ok"]
        assert app.diagnostics.snapshot()["input_samples"] == 0
        assert (await fixture.input_ack(prepared))["ok"]
        for _ in range(10):
            Stream.instances[-1].push()
        await eventually(lambda: app.diagnostics.snapshot()["input_samples"] > 0)
        assert not {"pcm", "text", "transcript"}.intersection(app.diagnostics.snapshot())
        assert calls == []
        assert host.session.playback.confirmed_sequence == -1
    finally:
        await app.aclose()
        await fixture.rig.transport.aclose()


@pytest.mark.parametrize("wrap", [
    lambda wire: "[" + wire + "]",
    lambda wire: "```json\n" + wire + "\n```",
    lambda wire: "~~~json\n" + wire + "\n~~~",
    lambda wire: "/* cancel */ " + wire,
    lambda wire: wire[:-1],
])
async def test_wrapped_cancel_is_rejected_without_stopping_active_response(monkeypatch, wrap):
    fixture = HostRig(monkeypatch)
    try:
        await fixture.start()
        response_id, _ = await fixture.response()
        message = fixture.message("cancel", response_id=response_id)
        result = await fixture.invoke(wrap(json.dumps(message)))
        assert result == {"ok": False, "reason": "invalid_control"}
        assert fixture.rig.session.active == response_id
        assert fixture.rig.session.playback.active == response_id
    finally:
        await fixture.host.aclose()


async def test_pending_open_rejects_other_track_and_preserves_original_request(monkeypatch):
    fixture = HostRig(monkeypatch)
    try:
        await fixture.start()
        prepared, original = await fixture.prepare(request_id="same-request")
        fixture.add_track("TR-other")
        replacement = {**original, "track_sid": "TR-other"}
        result = await fixture.invoke(json.dumps(replacement))
        assert result["ok"] is False
        assert result["reason"] == "input_conflict"
        assert (await fixture.input_ack(prepared))["ok"]
        assert fixture.rig.audio.backend.grant.track_sid == original["track_sid"]
        assert fixture.rig.audio.backend.grant.request_id == original["request_id"]
    finally:
        await fixture.host.aclose()


async def test_diagnostic_providers_generate_short_tone_without_transcribing():
    from local_gpt_live.connection_diagnostics import (
        DiagnosticCore, DiagnosticSynthesizer, DiagnosticTranscriber,
    )
    chunks = [chunk async for chunk in DiagnosticCore().stream("合成入力")]
    assert chunks
    data = await DiagnosticSynthesizer().synthesize("合成入力")
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 4800
        assert any(wav.readframes(4800))
    with pytest.raises(RuntimeError):
        await DiagnosticTranscriber().transcribe(bytes(320))


def test_diagnostic_pipeline_counts_without_retaining_audio_and_closes():
    from local_gpt_live.connection_diagnostics import ConnectionDiagnostics
    diagnostics = ConnectionDiagnostics()
    assert diagnostics.feed(bytes(320), start_sample=0) == ()
    assert diagnostics.snapshot() == {"input_samples": 160}
    diagnostics.reset()
    assert diagnostics.feed(bytes(640), start_sample=0) == ()
    assert diagnostics.snapshot() == {"input_samples": 480}
    with pytest.raises(ValueError):
        diagnostics.feed(bytes(2), start_sample=321)
    diagnostics.close()
    with pytest.raises(ValueError):
        diagnostics.feed(bytes(2), start_sample=320)
    with pytest.raises(RuntimeError):
        diagnostics.reset()


@pytest.mark.parametrize("action", ["enter", "stop", "eof"])
async def test_host_start_wait_can_be_confirmed_interrupted_or_closed(action):
    from local_gpt_live.browser_demo import wait_for_start
    read_fd, write_fd = os.pipe()
    stopped = asyncio.Event()
    try:
        with os.fdopen(read_fd) as stream:
            waiting = asyncio.create_task(wait_for_start(stopped, stream))
            await asyncio.sleep(0)
            if action == "enter":
                os.write(write_fd, b"\n")
            elif action == "stop":
                stopped.set()
            else:
                os.close(write_fd)
                write_fd = None
            assert await asyncio.wait_for(waiting, 1) is (action == "enter")
    finally:
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize("field,value", [
    ("stt_url", "http://localhost/private"),
    ("core_url", "https://example.invalid"),
    ("tts_url", "http://localhost?key=synthetic"),
])
def test_factory_validates_all_provider_urls_before_allocating_resources(monkeypatch, field, value):
    module = importlib.import_module("local_gpt_live.browser_demo")
    allocations = []
    def unexpected_pipeline():
        allocations.append("pipeline")
        raise AssertionError("設定拒否より前に資源を構築しない")
    monkeypatch.setattr(module, "VoiceInputPipeline", unexpected_pipeline)
    settings = dict(url="ws://127.0.0.1:7880", token="synthetic-test-token",
                    participant_identity="fixture-user", participant_sid="PA-fixture",
                    session_id="session-fixture", mode="conversation",
                    stt_url="http://127.0.0.1:18101", core_url="http://127.0.0.1:18102",
                    tts_url="http://127.0.0.1:18103", core_alias="fixture", speaker_id=3)
    settings[field] = value
    with pytest.raises(ValueError):
        module.create_demo(**settings)
    assert allocations == []


async def test_cli_confirmation_cancellation_closes_host_without_printing_token(monkeypatch, capsys):
    from argparse import Namespace
    module = importlib.import_module("local_gpt_live.browser_demo")
    fixture = HostRig(monkeypatch)
    created = []
    create = module.create_demo
    def observe_create(**settings):
        app = create(**settings)
        created.append(app)
        return app
    async def cancelled(stopped, stream):
        stopped.set()
        return False
    monkeypatch.setattr(module, "create_demo", observe_create)
    monkeypatch.setattr(module, "wait_for_start", cancelled)
    args = Namespace(url="ws://127.0.0.1:7880", participant_identity="fixture-user",
                     participant_sid="PA-fixture", host_identity="fixture-host",
                     session_id="session-fixture", mode="diagnostics",
                     stt_url=None, core_url=None, tts_url=None, core_alias=None, speaker_id=None)
    try:
        await module.serve(args, "synthetic-private-token")
        assert len(created) == 1
        assert fixture.participant.notifications == []
        with pytest.raises(ValueError):
            created[0].diagnostics.feed(bytes(320), start_sample=0)
        output = capsys.readouterr()
        assert "synthetic-private-token" not in output.out
        assert "synthetic-private-token" not in output.err
    finally:
        await fixture.rig.transport.aclose()
