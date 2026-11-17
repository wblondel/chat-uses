#!/usr/bin/env python3
"""Run a stream session and report on it (PLAN.md Phase 7).

    sudo infra/host/session.py start NAME   brain starts noting who's active in chat every minute;
                                            the containers' and the host's CPU counters are read
    sudo infra/host/session.py end          brain's report (posted in #ops too), plus what only the
                                            host sees: the encoder's speed and dropped frames
                                            (bcast's journal) and the CPU each container used.
                                            Printed and saved in /var/lib/chat-uses/sessions/
    sudo infra/host/session.py status       the session running, if any

The report is Markdown, ready for the session's retro note (docs/retros/).
Standard library only: it runs `incus` on the host.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VAR = Path(os.environ.get("CHAT_USES_VAR", "/var/lib/chat-uses"))
STATE = VAR / "session.json"
REPORTS = VAR / "sessions"
SLOW = 0.97  # an encoder report below this speed means the stream fell behind
_REPORT = re.compile(r"frames=(\d+) dropped=(\d+) duplicated=(\d+) speed=([\d.]+)x")


# --- the numbers (tested in test_session.py) -------------------------------------------


def encoder_stats(lines: list[str]) -> dict[str, Any]:
    """The encoder's 30-second reports (images/bcast/rootfs/usr/lib/chat-bcast/encode):
    speed, and frames dropped. Its counters start again when it restarts."""
    speeds: list[float] = []
    dropped = restarts = 0
    last: int | None = None
    for line in lines:
        match = _REPORT.search(line)
        if not match:
            continue
        drops, speed = int(match[2]), float(match[4])
        speeds.append(speed)
        if last is None:
            last = drops  # dropped before the session: not counted
            continue
        if drops < last:  # the encoder restarted: its count began again
            restarts += 1
            dropped += drops
        else:
            dropped += drops - last
        last = drops
    if not speeds:
        return {"reports": 0}
    return {
        "reports": len(speeds),
        "average": round(sum(speeds) / len(speeds), 3),
        "slowest": min(speeds),
        "slow": sum(s < SLOW for s in speeds),
        "dropped": dropped,
        "restarts": restarts,
    }


def cores(before_ns: int | None, after_ns: int | None, seconds: float) -> float | None:
    """CPUs' worth used on average: Incus's cpu.usage is nanoseconds of CPU time."""
    if before_ns is None or after_ns is None or seconds <= 0 or after_ns < before_ns:
        return None  # restarted in between, or not there
    return round((after_ns - before_ns) / 1e9 / seconds, 2)


def busy_percent(before: list[int], after: list[int]) -> float | None:
    """The host's CPU busy share from two /proc/stat readings (busy, total)."""
    total = after[1] - before[1]
    return round(100 * (after[0] - before[0]) / total, 1) if total > 0 else None


def render(brain: str, started: float, ended: float, encoder: dict[str, Any], cpu: dict[str, Any]) -> str:
    lines = [line for line in brain.strip().splitlines() if line.strip()]
    title = lines.pop(0) if lines and lines[0].startswith("📊") else (
        f"📊 Session from {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(started))}"
        f" to {time.strftime('%H:%M UTC', time.gmtime(ended))}"
    )
    if encoder.get("reports"):
        slow = f"{encoder['slow']} of {encoder['reports']} below {SLOW}x" if encoder["slow"] else "never behind"
        restarts = f", {encoder['restarts']} restarts" if encoder["restarts"] else ""
        lines.append(
            f"Encoder: average speed {encoder['average']}x, slowest {encoder['slowest']}x ({slow}),"
            f" {encoder['dropped']} frames dropped{restarts}"
        )
    else:
        lines.append("Encoder: no reports in bcast's journal for the session")
    parts = [f"host {cpu['host']} % busy"] if cpu.get("host") is not None else []
    parts += [f"{name} {n} CPUs" for name, n in cpu["containers"].items() if n is not None]
    lines.append("CPU on average: " + (", ".join(parts) if parts else "not known"))
    return "\n".join([f"## {title.removeprefix('📊 ')}", "", *[f"- {line}" for line in lines]]) + "\n"


# --- the host --------------------------------------------------------------------------


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def config(name: str) -> str:
    """A setting from host.env (with the example's defaults), as the bash scripts read it."""
    script = f'source "{HERE}/lib.sh"; load_config; printf %s "${name}"'
    return run("bash", "-c", script).stdout


def cpu_ns(name: str, project: str) -> int | None:
    try:
        state = json.loads(run("incus", "query", f"/1.0/instances/{name}/state?project={project}").stdout)
        return int(state["cpu"]["usage"])
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError):
        return None


def host_stat() -> list[int]:
    """busy and total jiffies of all CPUs, from /proc/stat."""
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    return [sum(fields) - idle, sum(fields)]


def containers() -> dict[str, str]:
    """Each container whose CPU is reported, and its project."""
    return {"bcast": "infra", "brain": "infra", config("TARGET_NAME") or "chatpc": "targets"}


def admin(*words: str) -> subprocess.CompletedProcess[str]:
    return run("incus", "exec", "brain", "--project", "infra", "--", "chat-brain-admin", *words, check=False)


def start(name: str) -> int:
    if STATE.exists():
        running = json.loads(STATE.read_text())
        print(f"session {running['name']!r} is running: end it first (session.py end)", file=sys.stderr)
        return 1
    answer = admin("session", "start", name)
    print((answer.stdout or answer.stderr).strip())
    if answer.returncode != 0:
        return 1
    state = {
        "name": name,
        "started": time.time(),
        "cpu": {c: cpu_ns(c, p) for c, p in containers().items()},
        "host": host_stat(),
    }
    VAR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    return 0


def end() -> int:
    if not STATE.exists():
        print("no session running (start one with session.py start NAME)", file=sys.stderr)
        return 1
    state = json.loads(STATE.read_text())
    ended = time.time()
    answer = admin("session", "end")
    brain = answer.stdout if answer.returncode == 0 else ""
    if answer.returncode != 0:
        print(f"brain: {(answer.stdout or answer.stderr).strip()}", file=sys.stderr)
    journal = run(
        "incus", "exec", "bcast", "--project", "infra", "--", "journalctl", "-u", "chat-bcast-encoder",
        "--since", f"@{int(state['started'])}", "-o", "cat", "--no-pager", check=False,
    ).stdout.splitlines()
    seconds = ended - state["started"]
    cpu = {
        "host": busy_percent(state["host"], host_stat()),
        "containers": {c: cores(state["cpu"].get(c), cpu_ns(c, p), seconds) for c, p in containers().items()},
    }
    report = render(brain, state["started"], ended, encoder_stats(journal), cpu)
    REPORTS.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", state["name"].lower()).strip("-") or "session"
    path = REPORTS / f"{time.strftime('%Y%m%d-%H%M', time.gmtime(state['started']))}-{slug}.md"
    path.write_text(report)
    STATE.unlink()
    print(report, end="")
    print(f"(saved in {path})")
    return 0


def status() -> int:
    if not STATE.exists():
        print("no session running")
        return 0
    state = json.loads(STATE.read_text())
    print(f"session {state['name']!r}: {round((time.time() - state['started']) / 60)} min")
    print((admin("session").stdout or "").strip())
    return 0


def main(argv: list[str]) -> int:
    match argv:
        case ["start", *name] if name:
            return start(" ".join(name))
        case ["end"]:
            return end()
        case ["status"]:
            return status()
        case _:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
