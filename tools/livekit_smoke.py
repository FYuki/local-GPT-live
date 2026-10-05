"""既存の空テスト Room 用 RTC スモーク。--run がなければ接続しない。

Ubuntu の既存 adapter 環境から実行する。SDK 1.1.16 と local_gpt_live が必要。
  uv run --no-sync python tools/livekit_smoke.py
  uv run --no-sync python tools/livekit_smoke.py --help

既存の有効tokenと隔離Roomを確認できた場合だけ、同じコマンドに --run --room <予約済みテストRoom名> を付ける。
ホストが LIVEKIT_URL、LIVEKIT_BACKEND_TOKEN、LIVEKIT_SYNTHETIC_TOKEN を既に供給していること。
token は値を表示せず、CLI 引数やリポジトリへ保存しない。本スクリプトは発行・署名しない。
URL は loopback の ws/wss のみ（既存dev例 ws://127.0.0.1:7880）。サービスを起動しない。
両 token は同室・別 identity、join / publish microphone / subscribe、残存期限60秒以上が必要。
JWT の読取は整合性検査だけであり、署名と権限は実接続時にサーバーが検証する。
管理者が事前に Room の隔離と両 identity の未使用を確認すること。join 後の空室検査では、
同 identity の既存接続を join 自体が置換する問題を防げない。

実行予算30秒、終了処理を含め通常最大37秒。失敗時は固定reasonだけを出力する。
合成PCM・偽provider・推論しないProbePipelineを使い、マイク、音声出力装置、GPU、HTTP推論を使わない。
出力準備確認は受信AudioStream設置後。受信PCMは保存せず、数量だけを集計する。
取消後にネットワーク内のPCMが届くことを許容し、実出音・ブラウザACK・停止遅延を推定しない。
実行準備済みでも、有効な既存tokenと隔離Roomが未確認なら --run を実行しない。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import io
import json
import math
import os
import struct
import sys
import time
import wave
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class SmokeFailure(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise SmokeFailure(reason)


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    require(parsed.scheme in {"ws", "wss"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and parsed.path in {"", "/"},
            "loopback_livekit_url_required")
    try:
        require(parsed.port is None or 0 < parsed.port <= 65535, "invalid_livekit_port")
    except ValueError:
        raise SmokeFailure("invalid_livekit_port") from None
    return value


@dataclass(frozen=True)
class Claims:
    room: str
    identity: str


def inspect_token(token: str) -> Claims:
    """署名検証ではない。接続先・権限の取り違えだけを事前拒否する。"""
    try:
        require(0 < len(token) <= 16384, "invalid_token_shape")
        parts = token.split(".")
        require(len(parts) == 3 and all(parts), "invalid_token_shape")
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        require(isinstance(payload, dict), "invalid_token_shape")
        grant = payload.get("video")
        require(isinstance(grant, dict), "token_video_grant_required")
        room, identity = grant.get("room"), payload.get("sub")
        require(isinstance(room, str) and 0 < len(room) <= 256
                and isinstance(identity, str) and 0 < len(identity) <= 256,
                "token_room_and_identity_required")
        require(not any(ord(c) < 32 for c in room + identity), "invalid_token_identity")
        require(grant.get("roomJoin") is True and grant.get("canPublish") is True
                and grant.get("canSubscribe") is True, "token_rtc_grants_required")
        sources = grant.get("canPublishSources")
        require(sources is None or (isinstance(sources, list) and "microphone" in sources),
                "token_microphone_grant_required")
        expires = payload.get("exp")
        require(type(expires) in {int, float} and math.isfinite(expires)
                and expires > time.time() + 60, "token_expiry_too_close")
        not_before = payload.get("nbf", 0)
        require(type(not_before) in {int, float} and math.isfinite(not_before)
                and not_before <= time.time(), "token_not_yet_valid")
        return Claims(room, identity)
    except SmokeFailure:
        raise
    except Exception:
        raise SmokeFailure("invalid_token_shape") from None


@dataclass(frozen=True)
class Config:
    url: str
    room: str
    backend_token: str = field(repr=False)
    synthetic_token: str = field(repr=False)
    backend: Claims
    synthetic: Claims


def configuration(environment: dict[str, str], room: str | None) -> Config:
    require(bool(room), "explicit_test_room_required")
    url = validate_url(environment.get("LIVEKIT_URL", ""))
    backend_token = environment.get("LIVEKIT_BACKEND_TOKEN", "")
    synthetic_token = environment.get("LIVEKIT_SYNTHETIC_TOKEN", "")
    require(bool(backend_token and synthetic_token), "two_existing_tokens_required")
    backend, synthetic = inspect_token(backend_token), inspect_token(synthetic_token)
    require(backend.room == synthetic.room == room, "token_room_mismatch")
    require(backend.identity != synthetic.identity, "distinct_identities_required")
    return Config(url, room, backend_token, synthetic_token, backend, synthetic)


def pcm(samples: int, *, tone: bool) -> bytes:
    return b"".join(struct.pack("<h", round(1000 * math.sin(2 * math.pi * 1000 * i / 16000))
                              if tone else 0) for i in range(samples))


def synthetic_wav(samples: int) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setparams((1, 2, 16000, samples, "NONE", "not compressed"))
        output.writeframes(pcm(samples, tone=True))
    return target.getvalue()


class ProbePipeline:
    """PCM を保存せず、VAD 推論をせず、位置と数量だけを観測する。"""

    def __init__(self) -> None:
        self.samples = self.nonzero = 0
        self.next_sample: int | None = None
        self.closed = False

    def reset(self, next_sample: int | None = None, *, quarantine: bool = False) -> None:
        self.next_sample = next_sample

    def feed(self, data: bytes, *, start_sample: int) -> tuple[()]:
        require(not self.closed and len(data) % 2 == 0, "invalid_probe_pcm")
        require(self.next_sample is None or self.next_sample == start_sample, "probe_sample_gap")
        values = [value for (value,) in struct.iter_unpack("<h", data)]
        self.samples += len(values)
        self.nonzero += sum(value != 0 for value in values)
        self.next_sample = start_sample + len(values)
        return ()

    def close(self) -> None:
        self.closed = True


class Providers:
    def __init__(self) -> None:
        self.stt_calls = 0
        self.long_cancelled = False

    async def transcribe(self, data: bytes) -> str:
        self.stt_calls += 1
        raise SmokeFailure("unexpected_stt_call")

    async def stream(self, text: str):
        if text == "long":
            yield "合成長区間。"
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.long_cancelled = True
                raise
        else:
            yield "合成一区間。"
            yield "合成二区間。"

    async def synthesize(self, text: str) -> bytes:
        return synthetic_wav(32000 if text == "合成長区間。" else 3200)


async def wait_until(predicate, reason: str, *, timeout: float = 8) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        require(asyncio.get_running_loop().time() < deadline, reason)
        await asyncio.sleep(0.01)


def check_readers(readers: list[asyncio.Task]) -> None:
    require(all(task.done() for task in readers), "output_reader_not_closed")
    require(all(not task.cancelled() and task.exception() is None for task in readers),
            "output_reader_failed")


async def run_smoke(config: Config) -> dict[str, int | bool]:
    # --run の入口以外から SDK / 本体を import しない。
    from livekit import rtc
    from local_gpt_live.input import AudioInput
    from local_gpt_live.livekit_transport import LiveKitConfig, LiveKitPlayback, LiveKitTransport
    from local_gpt_live.session import VoiceSession

    peer = rtc.Room()
    providers, probe, events, segments = Providers(), ProbePipeline(), [], []
    playback = LiveKitPlayback()
    session = VoiceSession(providers, providers, providers, playback, emit=events.append)
    audio = AudioInput(session, probe)
    transport = None
    source = mic = mic_publication = producer = None
    closing, tone, unexpected_peer = False, False, False
    owned, readers, streams = [], [], []
    announced, sinks, received, nonzero_received = {}, {}, {}, {}
    report: dict[str, int | bool] = {}

    def own(coroutine):
        task = asyncio.create_task(coroutine)
        owned.append(task)
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async def complete(task, reason: str, *, seconds: float = 10):
        done, _ = await asyncio.wait([task], timeout=seconds)
        require(bool(done), reason)
        return task.result()

    async def disconnect_peer() -> None:
        if peer.isconnected():
            await peer.disconnect()

    async def connect_peer() -> None:
        try:
            await peer.connect(config.url, config.synthetic_token,
                               rtc.RoomOptions(auto_subscribe=True))
        finally:
            if closing:
                await disconnect_peer()

    def authorize_ready(sid: str) -> None:
        if sid in announced and sid in sinks and transport is not None:
            require(transport.confirm_output_ready(announced[sid], sid), "output_ready_rejected")

    def published(item) -> None:
        announced[item.track_sid] = item.response_id
        authorize_ready(item.track_sid)

    def participant_connected(participant) -> None:
        nonlocal unexpected_peer, closing
        if participant.identity != config.backend.identity:
            unexpected_peer = True
            closing = True
            if transport is not None:
                transport.cancel()
                own(transport.aclose())
            own(disconnect_peer())

    async def consume_output(stream, sid: str) -> None:
        # reader が実行を開始してから準備完了を返す。最初の PCM は待たない。
        sinks[sid] = stream
        authorize_ready(sid)
        async for item in stream:
            values = [value for (value,) in struct.iter_unpack("<h", bytes(item.frame.data))]
            received[sid] = received.get(sid, 0) + len(values)
            nonzero_received[sid] = nonzero_received.get(sid, 0) + sum(value != 0 for value in values)

    def subscribed(track, publication, participant) -> None:
        if (transport is None or participant.identity != config.backend.identity
                or participant.sid != transport.room.local_participant.sid):
            return
        require(publication.name.startswith("ds-response-v1:"), "unexpected_backend_track")
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1,
                                 frame_size_ms=10, capacity=160)
        streams.append(stream)
        readers.append(own(consume_output(stream, publication.sid)))

    peer.on("participant_connected", participant_connected)
    peer.on("track_subscribed", subscribed)
    try:
        await complete(own(connect_peer()), "synthetic_connect_timeout")
        # 接続前の identity 未使用は管理者の確認事項。この検査で代替しない。
        require(not peer.remote_participants, "test_room_not_empty")
        require(peer.local_participant.identity == config.synthetic.identity,
                "synthetic_identity_mismatch")
        transport = LiveKitTransport(audio, LiveKitConfig(
            config.url, config.backend_token, peer.local_participant.identity,
            peer.local_participant.sid,
        ), on_track=published, on_segment=segments.append)
        await transport.connect()
        require(transport.room.local_participant.identity == config.backend.identity,
                "backend_identity_mismatch")
        require(set(transport.room.remote_participants) == {config.synthetic.identity},
                "unexpected_room_participant")
        source = rtc.AudioSource(16000, 1, queue_size_ms=100)
        mic = rtc.LocalAudioTrack.create_audio_track("local-gpt-live-smoke-input", source)

        async def publish_microphone():
            publication = await peer.local_participant.publish_track(
                mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=False))
            if closing and peer.isconnected():
                await peer.local_participant.unpublish_track(publication.sid)
            return publication

        mic_publication = await complete(own(publish_microphone()), "microphone_publish_timeout")

        async def produce() -> None:
            deadline = asyncio.get_running_loop().time()
            while not closing and peer.isconnected():
                await source.capture_frame(rtc.AudioFrame(pcm(160, tone=tone), 16000, 1, 160))
                deadline += 0.01
                await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))

        producer = own(produce())
        def microphone_available() -> bool:
            participant = transport.room.remote_participants.get(config.synthetic.identity)
            publication = participant.track_publications.get(mic_publication.sid) if participant else None
            return bool(publication and publication.subscribed and publication.track)

        await wait_until(microphone_available, "microphone_subscription_timeout")
        started = time.monotonic()
        grant = await transport.open_input(track_sid=mic_publication.sid,
                                           request_id="synthetic-open", revision=1)
        report["input_open_ms"] = round((time.monotonic() - started) * 1000)
        require(grant.track_sid == mic_publication.sid, "input_grant_mismatch")
        tone = True
        await wait_until(lambda: probe.nonzero >= 3200, "verified_input_pcm_timeout")
        tone = False
        report["verified_input_samples"] = probe.samples
        require(not unexpected_peer, "unexpected_room_participant")

        short = audio.submit_text("short", revision=2)
        await wait_until(lambda: sum(s.response_id == short for s in segments) == 2,
                         "short_output_timeout")
        short_segments = [s for s in segments if s.response_id == short]
        require([(s.sequence, s.sample_start, s.sample_end) for s in short_segments]
                == [(0, 0, 3200), (1, 3200, 6400)], "segment_metadata_mismatch")
        first_sid = short_segments[0].track_sid
        await wait_until(lambda: nonzero_received.get(first_sid, 0) > 0, "output_pcm_timeout")
        require(session.active == short, "unexpected_automatic_completion")
        long_response = audio.submit_text("long", revision=3)
        await wait_until(lambda: any(r == long_response for r in announced.values()),
                         "long_track_timeout")
        long_sid = next(sid for sid, response in announced.items() if response == long_response)
        await wait_until(lambda: nonzero_received.get(long_sid, 0) > 0, "long_output_pcm_timeout")
        require(not any(s.response_id == long_response for s in segments), "cancel_test_too_late")
        output = transport._output
        transport.cancel()
        require(session.active is None and playback.active is None and playback.pending_bytes == 0,
                "cancel_did_not_revoke_output")
        require(output is not None and output.track.muted and output.source.queued_duration == 0,
                "cancel_did_not_clear_local_source")
        await wait_until(lambda: transport._output is None, "cancel_retirement_timeout")
        require(providers.long_cancelled, "provider_cancel_not_observed")
        require(not any(s.response_id == long_response for s in segments), "cancelled_segment_sent")
        recovered = audio.submit_text("short", revision=4)
        await wait_until(lambda: sum(s.response_id == recovered for s in segments) == 2,
                         "recovered_output_timeout")
        recovered_sid = next(s.track_sid for s in segments if s.response_id == recovered)
        require(recovered_sid not in {first_sid, long_sid}, "response_track_reused")
        await wait_until(lambda: nonzero_received.get(recovered_sid, 0) > 0,
                         "recovered_output_pcm_timeout")
        require(not unexpected_peer and providers.stt_calls == 0, "unexpected_external_input")
        require(not any(e.kind == "playback_completed" for e in events), "unexpected_playback_ack")
        # 受信側に残る PCM 数から停止時刻や実出音を推定しない。
        closing = True
        source.clear_queue()
        mic.mute()
        await complete(producer, "producer_stop_timeout", seconds=2)
        await complete(own(disconnect_peer()), "synthetic_disconnect_timeout")
        await wait_until(lambda: transport._closed, "participant_disconnect_not_observed")
        await transport.aclose()
        await transport.aclose()
        await wait_until(lambda: not transport.room.isconnected() and transport._output is None
                         and (transport._pump is None or transport._pump.done())
                         and not transport.input._tasks and not transport.input._observations,
                         "adapter_cleanup_timeout")
        require(probe.closed and audio.backend.grant is None, "input_cleanup_incomplete")
        require(not any(e.kind in {"transport_failed", "shutdown_pending", "input_rejected"}
                        for e in events), "adapter_reported_failure")
        report.update(output_tracks=3, completed_segments=len(segments),
                      received_pcm_samples=sum(received.values()), cancel_pass=True,
                      recovery_pass=True, disconnect_pass=True, playback_ack_generated=False)
        return report
    finally:
        closing = True
        if source is not None:
            source.clear_queue()
        if mic is not None:
            mic.mute()
        cleanup = []
        if transport is not None:
            cleanup.append(own(transport.aclose()))
        else:
            cleanup.append(own(audio.close()))
        if producer is not None:
            cleanup.append(producer)
        for stream in streams:
            cleanup.append(own(stream.aclose()))
        if source is not None:
            cleanup.append(own(source.aclose()))
        cleanup.append(own(disconnect_peer()))
        cleanup.extend(task for task in owned if not task.done())
        if cleanup:
            _, pending = await asyncio.wait(set(cleanup), timeout=5)
            require(not pending, "smoke_cleanup_pending")
            require(not any(task.done() and not task.cancelled() and task.exception() is not None
                            for task in set(cleanup)), "smoke_cleanup_failed")
        require(not peer.isconnected(), "synthetic_room_not_closed")
        check_readers(readers)


async def bounded_run(config: Config) -> dict[str, int | bool]:
    try:
        async with asyncio.timeout(30):
            return await run_smoke(config)
    except TimeoutError:
        raise SmokeFailure("smoke_deadline_exceeded") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true", help="予約済みの空テスト Room へ実接続する")
    parser.add_argument("--room", help="管理者が確認したテスト Room 名と完全一致させる")
    args = parser.parse_args(argv)
    try:
        if args.run:
            config = configuration(dict(os.environ), args.room)
            result = asyncio.run(bounded_run(config))
            print(json.dumps({"status": "pass", **result}, ensure_ascii=False))
        else:
            if os.environ.get("LIVEKIT_URL"):
                validate_url(os.environ["LIVEKIT_URL"])
            tokens = [os.environ.get(name) for name in
                      ("LIVEKIT_BACKEND_TOKEN", "LIVEKIT_SYNTHETIC_TOKEN")]
            for token in filter(None, tokens):
                inspect_token(token)
            if all(tokens) and args.room:
                configuration(dict(os.environ), args.room)
            available = {}
            for name in ("livekit.rtc", "local_gpt_live.livekit_transport"):
                try:
                    available[name] = importlib.util.find_spec(name) is not None
                except ModuleNotFoundError:
                    available[name] = False
            print(json.dumps({"status": "offline", "connection_attempted": False,
                              "dependencies": available}, ensure_ascii=False))
            return 0 if all(available.values()) else 2
        return 0
    except SmokeFailure as error:
        print(json.dumps({"status": "fail", "reason": str(error)}), file=sys.stderr)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('{"status":"fail","reason":"interrupted"}', file=sys.stderr)
    except Exception:
        # native exception、endpoint、JWT、PCM は出力しない。
        print('{"status":"fail","reason":"smoke_operation_failed"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
