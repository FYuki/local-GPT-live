"""実hostとBackendをJSON-linesで呼ぶ合成fixture。RTC・推論は既存double。"""

import asyncio
import json
from pathlib import Path
import sys

import pytest
from livekit import rtc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_livekit_host import HostRig, Stream  # noqa: E402
from test_livekit_transport import eventually  # noqa: E402


async def main():
    patch = pytest.MonkeyPatch()
    fixture = HostRig(patch)
    controls = set()

    async def control(command):
        result = await fixture.invoke(json.dumps(command["message"]))
        print(json.dumps({"id": command["id"], "result": result}), flush=True)

    try:
        await fixture.start()
        await fixture.host._notifications.join()
        while raw := await asyncio.to_thread(sys.stdin.buffer.readline, 65537):
            command = json.loads(raw)
            name = command["command"]
            if name == "control":
                operation = asyncio.create_task(control(command))
                controls.add(operation)
                continue
            async with asyncio.timeout(5):
                if name == "initial":
                    result = fixture.participant.notifications[0][0]
                elif name == "publish":
                    track = fixture.add_track(command["track_sid"])
                    publication = fixture.remote.track_publications[track.sid]
                    if command.get("pending", False):
                        publication.subscribed, publication.track = False, None
                    fixture.rig.room.emit("track_published", publication, fixture.remote)
                    result = True
                elif name == "input_pending":
                    await eventually(lambda: bool(fixture.rig.room.listeners["track_subscribed"])
                                     or all(task.done() for task in controls))
                    result = any(not task.done() for task in controls)
                elif name == "subscribe":
                    from test_livekit_input import Track
                    publication = fixture.remote.track_publications[command["track_sid"]]
                    track = Track()
                    track.sid = publication.sid
                    publication.track, publication.subscribed = track, True
                    fixture.rig.room.emit("track_subscribed", track, publication, fixture.remote)
                    result = True
                elif name == "state":
                    from local_gpt_live.livekit_host import STATE_METHOD
                    data = rtc.RpcInvocationData(request_id="state-request", caller_identity="fixture-user",
                                                 payload=json.dumps(command["message"]), response_timeout=5)
                    result = json.loads(await fixture.participant.handlers[STATE_METHOD](data))
                elif name == "notifications":
                    await fixture.host._notifications.join()
                    result = [item[0] for item in fixture.participant.notifications]
                elif name == "output":
                    await eventually(lambda: bool(fixture.rig.published))
                    await fixture.host._notifications.join()
                    result = next(item[0] for item in reversed(fixture.participant.notifications)
                                  if item[0]["type"] == "output_track")
                elif name == "push":
                    before = len(fixture.rig.pipeline.received)
                    for _ in range(10):
                        Stream.instances[-1].push()
                    await eventually(lambda: len(fixture.rig.pipeline.received) == before + 10)
                    result = len(fixture.rig.pipeline.received)
                elif name == "status":
                    result = {"received": len(fixture.rig.pipeline.received),
                              "readers": len(Stream.instances),
                              "confirmed": fixture.rig.session.playback.confirmed_sequence,
                              "capture_calls": sum(s.capture_calls for s in fixture.rig.sources)}
                elif name == "estimate":
                    await eventually(lambda: bool(fixture.rig.segments))
                    output = fixture.rig.transport._output
                    fixture.rig.now_ns = output.progress.next_estimated_complete_at_ns()
                    fixture.rig.transport._finish_estimated_output(output)
                    await fixture.host._notifications.join()
                    result = next(item[0] for item in reversed(fixture.participant.notifications)
                                  if item[0]["type"] == "output_estimated_completed")
                elif name == "close":
                    result = True
                else:
                    raise ValueError("unknown_fixture_command")
            print(json.dumps({"id": command["id"], "result": result}), flush=True)
            if name == "close":
                break
    finally:
        for operation in controls:
            operation.cancel()
        await asyncio.gather(*controls, return_exceptions=True)
        for gate in fixture.rig.gates:
            gate.release.set()
        if fixture.host is not None:
            await fixture.host.aclose()
        await fixture.rig.transport.aclose()
        patch.undo()


if __name__ == "__main__":
    asyncio.run(main())
