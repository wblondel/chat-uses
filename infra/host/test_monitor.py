"""Tests for monitor.py: what's wrong, when #ops hears it, and whole rounds against a fake
incus, a fake brain and a fake Discord webhook.

    python3 -m unittest discover -s infra/host -v
"""

import contextlib
import http.server
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import monitor

REPORT = "frames=900 dropped=0 duplicated=0 speed={}x (last 30 s)"
HEALTHY = {"ok": True, "chat": {"youtube": {"state": "streaming"}, "discord": {"state": "connected"}}}
ALL_UP = dict.fromkeys(monitor.INFRA, True)
BRAIN_DOWN = "brain doesn't answer (/healthz): no chat, no overlay, no alerts from it"


def problems(**changes: Any) -> dict[str, str]:
    values: dict[str, Any] = {
        "health": HEALTHY,
        "running": ALL_UP,
        "encoder": [REPORT.format("1.000")],
        "disk": 0.5,
        "pool": 0.5,
        "steal": 0.01,
    }
    return monitor.problems(**(values | changes))


class WhatsWrong(unittest.TestCase):
    def test_all_good(self) -> None:
        self.assertEqual(problems(), {})
        self.assertEqual(problems(encoder=None, disk=None, pool=None, steal=None), {})  # unknown isn't wrong

    def test_brain(self) -> None:
        self.assertEqual(list(problems(health=None)), ["brain"])
        down = ALL_UP | {"brain": False}  # its container stopped: one problem, not two
        self.assertEqual(list(problems(health=None, running=down)), ["container brain"])

    def test_chat(self) -> None:
        youtube = {"state": "reconnecting", "reason": "UNAVAILABLE"}
        found = problems(
            health={"chat": {"youtube": youtube, "discord": {"state": "stopped", "reason": "bad token"}}}
        )
        self.assertEqual(found["youtube"], "YouTube chat is reconnecting: UNAVAILABLE")
        self.assertEqual(found["discord"], "Discord is stopped: bad token")
        errors = {
            "state": "looking",
            "reason": "",
            "recent_errors": ["videos.list: HTTP 500", "videos.list: HTTP 503"],
        }
        self.assertEqual(
            problems(health={"chat": {"youtube": errors}})["youtube"],
            "YouTube chat is looking: videos.list: HTTP 503",
        )
        for normal in ("off", "waiting", "streaming"):  # waiting: for the quota, or for the stream to go live
            self.assertEqual(
                problems(health={"chat": {"youtube": {"state": normal}, "discord": {"state": "off"}}}), {}
            )

    def test_containers(self) -> None:
        found = problems(running=ALL_UP | {"egress": False, "intranet": False})
        self.assertEqual(sorted(found), ["container egress", "container intranet"])
        self.assertEqual(found["container egress"], "the egress container isn't running")

    def test_encoder(self) -> None:
        self.assertIn("hasn't reported", problems(encoder=["Input #0, rtsp"])["encoder silent"])
        slow = [REPORT.format("1.000"), REPORT.format("0.910")]
        self.assertEqual(
            problems(encoder=slow)["encoder slow"], "the encoder is behind real time (speed 0.91x)"
        )
        self.assertEqual(problems(encoder=[REPORT.format("0.900"), REPORT.format("1.001")]), {})  # caught up

    def test_disk_pool_steal(self) -> None:
        found = problems(disk=0.93, pool=0.86, steal=0.25)
        self.assertEqual(found["disk"], "the host's disk is 93% full")
        self.assertEqual(found["pool"], "Incus's storage pool is 86% full")
        self.assertEqual(found["steal"], "CPU steal is 25%: the VPS's neighbours are taking our CPU")
        self.assertEqual(problems(disk=0.89, pool=0.84, steal=0.09), {})

    def test_steal_share(self) -> None:
        self.assertEqual(monitor.steal_share([100, 10_000], [400, 13_000]), 0.1)
        self.assertIsNone(monitor.steal_share(None, [400, 13_000]))  # the first round
        self.assertIsNone(monitor.steal_share([100, 10_000], [0, 500]))  # rebooted in between


class Settings(unittest.TestCase):
    def test_from_host_env_or_once_installed_from_monitor_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host_env = Path(tmp, "host.env")
            host_env.write_text("BRAIN_PORT=9000\n")
            with mock.patch.dict(os.environ, {"CHAT_USES_CONFIG": str(host_env)}):
                found = monitor.config("BRAIN_IP", "BRAIN_PORT", "POOL_NAME")
            self.assertEqual(found, {"BRAIN_IP": "10.20.0.11", "BRAIN_PORT": "9000", "POOL_NAME": "chat"})
            installed = Path(tmp, "monitor.env")
            installed.write_text("BRAIN_IP=10.0.0.5\nBRAIN_PORT=8080\nPOOL_NAME=\n")
            with (
                mock.patch.object(monitor, "HERE", Path(tmp)),
                mock.patch.object(monitor, "SETTINGS", installed),
            ):
                found = monitor.config("BRAIN_IP", "BRAIN_PORT", "POOL_NAME")
                self.assertEqual(found, {"BRAIN_IP": "10.0.0.5", "BRAIN_PORT": "8080", "POOL_NAME": ""})
                installed.unlink()
                self.assertEqual(monitor.config("BRAIN_IP"), {})


class WhenOpsHears(unittest.TestCase):
    def test_after_the_grace_once_then_back_to_normal(self) -> None:
        state: dict[str, Any] = {}
        yt = {"youtube": "YouTube chat is stopped: quota"}
        posts, state = monitor.advance(state, yt, 1000)
        self.assertEqual(posts, [])  # 5 minutes of grace
        posts, state = monitor.advance(state, yt, 1299)
        self.assertEqual(posts, [])
        posts, state = monitor.advance(state, yt, 1300)
        self.assertEqual(posts, [("youtube", "🚨 YouTube chat is stopped: quota")])
        posts, state = monitor.advance(state, yt, 1360)
        self.assertEqual(posts, [])  # told once
        posts, state = monitor.advance(state, {}, 1900)
        self.assertEqual(posts, [])  # gone for 2 minutes first
        posts, state = monitor.advance(state, {}, 2020)
        self.assertEqual(posts, [(None, "✅ Back to normal after 15 min: YouTube chat is stopped: quota")])
        self.assertEqual(state["problems"], {})

    def test_no_flapping(self) -> None:
        state: dict[str, Any] = {}
        slow = {"encoder slow": "the encoder is behind real time (speed 0.95x)"}
        posts, state = monitor.advance(state, slow, 1000)
        posts, state = monitor.advance(state, slow, 1180)
        self.assertEqual(posts, [("encoder slow", "🚨 the encoder is behind real time (speed 0.95x)")])
        posts, state = monitor.advance(state, {}, 1240)  # one good report
        posts, state = monitor.advance(state, slow, 1300)  # slow again: not told again
        self.assertEqual(posts, [])
        posts, state = monitor.advance(state, {}, 1360)
        posts, state = monitor.advance(state, {}, 1420)
        self.assertEqual(posts, [])
        posts, state = monitor.advance(state, {}, 1480)
        self.assertEqual(
            posts, [(None, "✅ Back to normal after 6 min: the encoder is behind real time (speed 0.95x)")]
        )

    def test_a_short_problem_is_never_told(self) -> None:
        state: dict[str, Any] = {"stat": [1, 2]}
        posts, state = monitor.advance(state, {"brain": "down"}, 1000)
        posts, state = monitor.advance(state, {}, 1060)
        self.assertEqual(posts, [])
        self.assertEqual(state, {"stat": [1, 2], "problems": {}})  # the rest of the state is kept

    def test_disk_at_once_and_the_text_follows(self) -> None:
        posts, state = monitor.advance({}, {"disk": "the host's disk is 91% full"}, 1000)
        self.assertEqual(posts, [("disk", "🚨 the host's disk is 91% full")])
        posts, state = monitor.advance(state, {"disk": "the host's disk is 95% full"}, 1060)
        self.assertEqual(posts, [])
        posts, state = monitor.advance(state, {}, 1120)
        posts, state = monitor.advance(state, {}, 1240)
        self.assertEqual(posts, [(None, "✅ Back to normal after 2 min: the host's disk is 95% full")])

    def test_snooze(self) -> None:
        state: dict[str, Any] = {"snooze_until": 1500}
        brain = {"brain": "down"}
        posts, state = monitor.advance(state, brain, 1000)
        posts, state = monitor.advance(state, brain, 1200)
        self.assertEqual(posts, [])  # past its grace, but snoozed
        posts, state = monitor.advance(state, brain, 1500)
        self.assertEqual(posts, [("brain", "🚨 down")])  # still down when the snooze ends
        posts, _ = monitor.advance({"snooze_until": 9999}, {}, 1600)
        self.assertEqual(posts, [])

    def test_the_state_given_is_not_changed(self) -> None:
        state: dict[str, Any] = {"problems": {"brain": {"since": 0, "text": "down", "told": False}}}
        monitor.advance(state, {"brain": "down"}, 1000)
        self.assertFalse(state["problems"]["brain"]["told"])


FAKE_INCUS = r"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
down = Path(__file__).with_name("down").read_text().split()
if args[0] == "query" and args[1].startswith("/1.0/instances/"):
    name = args[1].split("/")[3]
    if name == "intranet" or "gone:" + name in down:
        sys.exit("Error: Instance not found")  # intranet: not deployed here
    print(json.dumps({"status": "Stopped" if name in down else "Running"}))
elif args[0] == "query" and args[1] == "/1.0/storage-pools/chat/resources":
    print(json.dumps({"space": {"used": 30, "total": 100}}))
elif args[:2] == ["exec", "bcast"] and "chat-bcast-encoder" in args:
    if "encoder" not in down:
        print("frames=900 dropped=0 duplicated=0 speed=1.000x (last 30 s)")
else:
    sys.exit("unexpected: " + " ".join(args))
"""


class Recorder(http.server.BaseHTTPRequestHandler):
    """brain's /healthz (GET) and Discord's webhook (POST)."""

    health: ClassVar[dict[str, Any] | None] = HEALTHY
    posts: ClassVar[list[dict[str, Any]]] = []
    refuse: ClassVar[bool] = False

    def do_GET(self) -> None:
        if self.health is None:
            self.send_error(503)
            return
        body = json.dumps(self.health).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.refuse:
            self.send_error(429)
            return
        Recorder.posts.append(data)
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextlib.contextmanager
def server() -> Iterator[int]:
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class WholeRounds(unittest.TestCase):
    def setUp(self) -> None:
        Recorder.health, Recorder.posts, Recorder.refuse = HEALTHY, [], False
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        port = self.enterContext(server())
        bin_dir, self.var, secrets = Path(tmp, "bin"), Path(tmp, "var"), Path(tmp, "secrets")
        bin_dir.mkdir()
        secrets.mkdir()
        fake = bin_dir / "incus"
        fake.write_text(FAKE_INCUS)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.down = bin_dir / "down"
        self.down.write_text("")
        self.webhook = secrets / "ops-webhook"
        self.webhook.write_text(f"http://127.0.0.1:{port}/api/webhooks/1/secret\n")
        host_env = Path(tmp, "host.env")
        host_env.write_text(f"BRAIN_IP=127.0.0.1\nBRAIN_PORT={port}\n")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CHAT_USES_CONFIG": str(host_env),
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        self.enterContext(mock.patch.dict(os.environ, env))
        self.enterContext(mock.patch.object(monitor, "VAR", self.var))
        self.enterContext(mock.patch.object(monitor, "STATE", self.var / "monitor.json"))
        self.enterContext(mock.patch.object(monitor, "WEBHOOK", self.webhook))
        self.enterContext(mock.patch.object(monitor, "disk_used", lambda path="/": 0.4))
        self.clock = 1_800_000_000.0
        self.enterContext(mock.patch.object(monitor.time, "time", lambda: self.clock))

    def round_(self, minutes: float = 1) -> list[str]:
        """One round, `minutes` after the last: what #ops got."""
        self.clock += 60 * minutes
        before = len(Recorder.posts)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(monitor.main([]), 0)
        return [post["content"] for post in Recorder.posts[before:]]

    def test_quiet_when_all_is_well(self) -> None:
        for _ in range(3):
            self.assertEqual(self.round_(), [])
        state = json.loads((self.var / "monitor.json").read_text())
        self.assertEqual(state["problems"], {})
        self.assertEqual(len(state["stat"]), 2)

    def test_brain_down_then_back(self) -> None:
        self.round_()
        Recorder.health = None
        self.assertEqual(self.round_(), [])
        self.assertEqual(self.round_(), [])
        told = self.round_()  # 2 minutes down
        self.assertEqual(told, [f"🚨 {BRAIN_DOWN}"])
        self.assertEqual(Recorder.posts[-1]["allowed_mentions"], {"parse": []})  # never pings anyone
        self.assertEqual(self.round_(), [])
        Recorder.health = HEALTHY
        self.assertEqual(self.round_(), [])
        self.assertEqual(self.round_(), [])
        self.assertEqual(
            self.round_(), [f"✅ Back to normal after 4 min: {BRAIN_DOWN}"]
        )  # back for 2 minutes

    def test_a_stopped_container_and_a_silent_encoder(self) -> None:
        self.down.write_text("egress encoder")
        self.round_()
        self.assertEqual(self.round_(), [])
        self.assertEqual(self.round_(), ["🚨 the egress container isn't running"])  # 2 minutes
        silent = "🚨 the encoder hasn't reported for 2 minutes: is the stream running?"
        self.assertEqual(self.round_(), [silent])  # 3 minutes

    def test_brain_gone(self) -> None:
        self.down.write_text("gone:brain")  # a redeploy deleted it and didn't make it again
        Recorder.health = None
        told = [self.round_() for _ in range(4)]
        self.assertEqual(told, [[], [], ["🚨 the brain container isn't running"], []])  # not "doesn't answer"

    def test_a_refused_post_is_told_next_round(self) -> None:
        self.down.write_text("egress")
        self.round_()
        self.round_()
        Recorder.refuse = True
        self.assertEqual(self.round_(), [])
        Recorder.refuse = False
        self.assertEqual(self.round_(), ["🚨 the egress container isn't running"])

    def test_the_webhook_url_is_never_printed(self) -> None:
        self.down.write_text("egress")
        self.round_()
        self.round_()
        Recorder.refuse = True
        self.clock += 60
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            monitor.main([])
        self.assertIn("Discord refused the post (HTTPError)", err.getvalue())
        self.assertNotIn("secret", err.getvalue())

    def test_check_snooze_and_test(self) -> None:
        self.down.write_text("egress")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(monitor.main(["--check"]), 0)
        self.assertIn("monitor: the egress container isn't running (told after 120 s)", out.getvalue())
        self.assertIn("monitor: webhook set", out.getvalue())
        self.assertFalse((self.var / "monitor.json").exists())  # --check remembers nothing
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(["--snooze", "3"]), 0)
        for _ in range(2):
            self.assertEqual(self.round_(), [])  # past its grace, but snoozed
        self.assertEqual(self.round_(), ["🚨 the egress container isn't running"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(["--test"]), 0)
        self.assertIn("reach #ops", Recorder.posts[-1]["content"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(monitor.main(["--snooze", "soon"]), 2)

    def test_no_webhook(self) -> None:
        self.webhook.unlink()
        self.down.write_text("egress")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(monitor.main(["--test"]), 1)
        self.assertIn("no webhook in", err.getvalue())


if __name__ == "__main__":
    unittest.main()
