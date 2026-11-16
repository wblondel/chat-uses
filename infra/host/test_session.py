"""Tests for session.py: the numbers, and a whole session against a fake incus.

    python3 -m unittest discover -s infra/host -v
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session  # noqa: E402

LINE = "frames={f} dropped={d} duplicated=0 speed={s}x (last 30 s)"


class Numbers(unittest.TestCase):
    def test_encoder_reports_speeds_and_drops_across_a_restart(self) -> None:
        lines = [
            "Input #0, rtsp, from 'rtsp://127.0.0.1:8554/live':",  # not a report
            LINE.format(f=900, d=5, s="1.000"),  # drops before the session aren't counted
            LINE.format(f=1800, d=7, s="0.950"),
            LINE.format(f=2700, d=7, s="1.010"),
            LINE.format(f=300, d=1, s="1.000"),  # restarted: its count began again
            LINE.format(f=1200, d=3, s="1.000"),
        ]
        self.assertEqual(
            session.encoder_stats(lines),
            {"reports": 5, "average": 0.992, "slowest": 0.95, "slow": 1, "dropped": 5, "restarts": 1},
        )
        self.assertEqual(session.encoder_stats(["nothing here"]), {"reports": 0})

    def test_cpu(self) -> None:
        self.assertEqual(session.cores(0, 3_000_000_000_000, 2000), 1.5)
        self.assertIsNone(session.cores(5, 1, 10))  # restarted in between
        self.assertIsNone(session.cores(None, 1, 10))
        self.assertEqual(session.busy_percent([100, 1000], [400, 2000]), 30.0)
        self.assertIsNone(session.busy_percent([1, 1], [1, 1]))

    def test_the_report_is_markdown_for_the_retro(self) -> None:
        brain = "📊 Session 'Beta 1': 2 h 00 min (2027-01-15, 08:00 UTC to 10:00 UTC)\nViewers (YouTube): peak 30\n"
        encoder = {"reports": 240, "average": 1.0, "slowest": 0.99, "slow": 0, "dropped": 2, "restarts": 0}
        cpu = {"host": 41.5, "containers": {"bcast": 1.6, "brain": 0.1, "chatpc": None}}
        self.assertEqual(
            session.render(brain, 0, 7200, encoder, cpu),
            "## Session 'Beta 1': 2 h 00 min (2027-01-15, 08:00 UTC to 10:00 UTC)\n\n"
            "- Viewers (YouTube): peak 30\n"
            "- Encoder: average speed 1.0x, slowest 0.99x (never behind), 2 frames dropped\n"
            "- CPU on average: host 41.5 % busy, bcast 1.6 CPUs, brain 0.1 CPUs\n",
        )
        empty = session.render("", 1_800_000_000, 1_800_003_600, {"reports": 0}, {"host": None, "containers": {}})
        self.assertIn("## Session from 2027-01-15 08:00 UTC to 09:00 UTC", empty)
        self.assertIn("- Encoder: no reports", empty)
        self.assertIn("- CPU on average: not known", empty)


FAKE_INCUS = r"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
counter = Path(__file__).with_name("cpu")
if args[0] == "query":
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 60_000_000_000))  # each look: one more minute of CPU
    print(json.dumps({"cpu": {"usage": n}}))
elif args[:2] == ["exec", "brain"]:
    words = args[args.index("--") + 2:]
    if words[:2] == ["session", "start"]:
        print("Session " + repr(" ".join(words[2:])) + " started")
    elif words == ["session", "end"]:
        print("📊 Session 'Beta 1': 1 min (2027-01-15, 08:00 UTC to 08:01 UTC)")
        print("Commands: 3 (3.0 a minute): 2 accepted, 1 refused (invalid 1)")
    else:
        print("Session 'Beta 1': 1 min, 1 samples.")
elif args[:2] == ["exec", "bcast"]:
    print("frames=900 dropped=0 duplicated=0 speed=1.000x (last 30 s)")
    print("frames=1800 dropped=2 duplicated=0 speed=0.990x (last 30 s)")
else:
    sys.exit("unexpected: " + " ".join(args))
"""


class WholeSession(unittest.TestCase):
    def test_start_then_end_against_a_fake_incus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, var = Path(tmp, "bin"), Path(tmp, "var")
            bin_dir.mkdir()
            fake = bin_dir / "incus"
            fake.write_text(FAKE_INCUS)
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CHAT_USES_CONFIG": str(Path(tmp, "none"))}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(session, "VAR", var),
                mock.patch.object(session, "STATE", var / "session.json"),
                mock.patch.object(session, "REPORTS", var / "sessions"),
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(session.main(["start", "Beta", "1"]), 0)
                self.assertIn("Session 'Beta 1' started", out.getvalue())
                state = json.loads((var / "session.json").read_text())
                self.assertEqual(set(state["cpu"]), {"bcast", "brain", "chatpc"})
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(session.main(["start", "Beta 2"]), 1)
                self.assertIn("is running", err.getvalue())
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(session.main(["end"]), 0)
                report = out.getvalue()
                self.assertIn("## Session 'Beta 1': 1 min", report)
                self.assertIn("- Commands: 3", report)
                self.assertIn("- Encoder: average speed 0.995x, slowest 0.99x (never behind), 2 frames dropped", report)
                self.assertIn("bcast", report.split("- CPU on average: ")[1])
                [saved] = (var / "sessions").iterdir()
                self.assertTrue(saved.name.endswith("-beta-1.md"))
                self.assertFalse((var / "session.json").exists())
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(session.main(["end"]), 1)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(session.main(["nonsense"]), 2)


if __name__ == "__main__":
    unittest.main()
