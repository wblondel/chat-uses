"""Does the stream reach YouTube? bcast's report, and what #ops hears."""

import asyncio
import os
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from aiohttp import test_utils

from chatuses.adapters.discord_bot import DiscordChat
from chatuses.brain import BCAST_SILENT, Brain, Config
from chatuses.executor import connect_to
from chatuses.store import Store

REPO = Path(__file__).resolve().parents[2]
REPORTER = REPO / "images/bcast/rootfs/usr/lib/chat-bcast/report"
T = 1_800_000_000.0


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def brain_in(store: Store) -> Brain:
    brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
    brain.discord = DiscordChat("token", 1, "control", lambda _m: None, lambda: "")
    return brain


def ops(brain: Brain) -> list[str]:
    assert brain.discord is not None
    return [text for text, _i, ch, _h in brain.discord._alerts if ch == "ops"]  # pyright: ignore[reportPrivateUsage]


def test_ops_hears_after_a_minute_of_failing_once_and_when_it_s_back() -> None:
    with Store(":memory:") as store:
        brain = brain_in(store)
        assert brain.stream_line() == "Stream: no report from bcast yet"
        brain.forwarder_report("ok", 0, T)
        brain.forwarder_report("failing", 30, T + 30)
        assert ops(brain) == []  # a restart or two: not yet
        brain.forwarder_report("failing", 60, T + 60)
        brain.forwarder_report("failing", 75, T + 75)
        [alert] = ops(brain)
        assert alert.startswith(
            "📡 The stream isn't reaching YouTube: bcast's forwarder has been failing for 60 s."
        )
        assert "youtube-stream-key" in alert and "images/bcast/deploy.sh" in alert
        brain.forwarder_report("ok", 0, T + 90)
        assert ops(brain)[-1] == "📡 The stream reaches YouTube again."
        brain.forwarder_report("failing", 600, T + 700)  # failing again: said again
        brain.forwarder_report("idle", 0, T + 710)
        said = ops(brain)
        assert len(said) == 4 and said[2].startswith("📡 The stream isn't reaching YouTube")
        assert said[3] == "📡 bcast sends nothing to YouTube now (no stream key)."


def test_ops_hears_when_bcast_goes_quiet_once() -> None:
    with Store(":memory:") as store:
        brain = brain_in(store)
        brain.bcast_silence(T)
        assert ops(brain) == []  # never heard from: an older bcast, nothing to say
        brain.forwarder_report("ok", 0, T)
        brain.bcast_silence(T + BCAST_SILENT - 1)
        assert ops(brain) == []
        brain.bcast_silence(T + BCAST_SILENT)
        brain.bcast_silence(T + BCAST_SILENT + 60)
        [alert] = ops(brain)
        assert alert.startswith("📡 No word from bcast for 3 min: the stream may be down.")
        brain.forwarder_report("ok", 0, T + 400)
        assert ops(brain)[-1] == "📡 The stream reaches YouTube again."


def test_the_status_line_says_where_the_stream_is() -> None:
    with Store(":memory:") as store:
        brain = brain_in(store)
        now = time.time()
        brain.forwarder_report("ok", 0, now)
        assert brain.stream_line() == "Stream: reaching YouTube"
        assert "Stream: reaching YouTube" in brain.status_text()
        brain.forwarder_report("failing", 95, now)
        assert brain.stream_line() == "Stream: NOT reaching YouTube (the forwarder has failed for 95 s)"
        brain.forwarder_report("idle", 0, now)
        assert brain.stream_line() == "Stream: not sent to YouTube (no stream key in bcast)"
        brain.forwarder_report("ok", 0, now - 600)
        assert brain.stream_line() == "Stream: no word from bcast for 10 min"


def test_only_bcast_may_report_and_only_what_it_reports() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_in(store)
            async with test_utils.TestClient(test_utils.TestServer(brain.web_app())) as client:
                good = {"state": "ok", "failing_s": 0, "restarts": 2}
                assert (await client.put("/bcast/forwarder", json=good)).status == 403  # no BCAST_IP
                brain.bcast_ip = "10.20.0.10"
                assert (await client.put("/bcast/forwarder", json=good)).status == 403  # not from bcast
                brain.bcast_ip = "127.0.0.1"
                assert (await client.put("/bcast/forwarder", json=good)).status == 204
                assert brain.forwarder is not None and brain.forwarder["state"] == "ok"
                bad_ones: list[object] = [
                    [1],
                    {"state": "on fire"},
                    {"state": "failing", "failing_s": -1},
                    {"state": "failing", "failing_s": True},
                    {"state": "ok", "failing_s": "9"},
                ]
                for bad in bad_ones:
                    assert (await client.put("/bcast/forwarder", json=bad)).status == 400, bad
                assert (await client.put("/bcast/forwarder", data=b"not json")).status == 400
                assert (await client.get("/bcast/forwarder")).status == 405

    run(test)


def test_bcast_s_address_is_checked() -> None:
    assert Config.from_env({"TARGET_HOST": "x", "BCAST_IP": "10.20.0.10"}).bcast_ip == "10.20.0.10"
    with pytest.raises(SystemExit):
        Config.from_env({"TARGET_HOST": "x", "BCAST_IP": "bcast"})


FAKE_SYSTEMCTL = """#!/bin/sh
# systemctl show, as bcast's reporter asks it: the state in $STATE_FILE
cat "$STATE_FILE"
"""


@pytest.mark.skipif(sys.platform != "linux", reason="reads CLOCK_MONOTONIC as systemd does")
def test_bcast_s_reporter_tells_brain_what_systemd_says(tmp_path: Path) -> None:
    """The real reporter (images/bcast), a fake systemctl, brain's real web app."""

    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_in(store)
            brain.bcast_ip = "127.0.0.1"
            server = test_utils.TestServer(brain.web_app())
            await server.start_server()
            port = server.make_url("/").port or 0
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
            (bin_dir / "systemctl").chmod(0o755)
            state_file = tmp_path / "state"
            started_long_ago = max(1, int((time.monotonic() - 60) * 1e6))  # a minute ago (or at boot)
            state_file.write_text(
                f"ActiveState=active\nActiveEnterTimestampMonotonic={started_long_ago}\nNRestarts=0\n"
            )
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "STATE_FILE": str(state_file),
                "BRAIN_URL": f"ws://127.0.0.1:{port}/ws/overlay",
                "FORWARD_URL": "rtmp://127.0.0.1:1935/dryrun/test",  # something to send to
                "REPORT_EVERY": "0.1",
            }
            proc = await asyncio.create_subprocess_exec(sys.executable, str(REPORTER), env=env)
            try:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if brain.forwarder is not None:
                        break
                assert brain.forwarder is not None and brain.forwarder["state"] == "ok"
                just_now = int(time.monotonic() * 1e6)  # restarted a moment ago, and again
                state_file.write_text(
                    f"ActiveState=activating\nActiveEnterTimestampMonotonic={just_now}\nNRestarts=7\n"
                )
                await asyncio.sleep(0.5)
                assert brain.forwarder["state"] == "failing"
                assert 0 <= brain.forwarder["failing_s"] <= 1
            finally:
                proc.terminate()
                await proc.wait()
                await server.close()
            env["FORWARD_URL"] = ""  # no key file on this machine either: idle
            proc = await asyncio.create_subprocess_exec(sys.executable, str(REPORTER), env=env)
            server = test_utils.TestServer(
                brain.web_app(), port=int(env["BRAIN_URL"].split(":")[2].split("/")[0])
            )
            await server.start_server()
            try:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if brain.forwarder["state"] == "idle":
                        break
                assert brain.forwarder["state"] == "idle"
            finally:
                proc.terminate()
                await proc.wait()
                await server.close()

    run(test)
