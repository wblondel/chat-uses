#!/usr/bin/env python3
"""Watch the whole stream from the host and tell #ops when something is wrong (PLAN.md Phase 10).

    sudo infra/host/monitor.py           one round (the chat-uses-monitor timer: every minute)
    sudo infra/host/monitor.py --check   what it sees now; nothing sent, nothing remembered
    sudo infra/host/monitor.py --test    post a test message in #ops (the webhook works)
    sudo infra/host/monitor.py --snooze 30
                                         nothing new told for 30 minutes (maintenance,
                                         a redeploy); 0 ends it. Problems still lasting
                                         then are told

It runs on the host, not in brain, so it still speaks when brain is down. Its
alerts go to #ops through a Discord webhook: the URL, on one line, in
/etc/chat-uses/secrets/ops-webhook (Discord: #ops → Edit Channel →
Integrations → Webhooks → New Webhook → Copy Webhook URL). A problem is told
once it has lasted its grace time (GRACE), and "back to normal" once it has
been gone for CLEAR; what it has told is kept in /var/lib/chat-uses/monitor.json.
A post that fails is tried again next round.

What it looks at:
- brain: its /healthz answers (BRAIN_IP:BRAIN_PORT); from it, YouTube and
  Discord chat: stopped, ended, reconnecting, or still looking (YouTube
  waiting for its quota or for the stream to go live is normal: brain
  already says so in #ops; "off", no video or no token set, is a choice);
- the infrastructure containers run (brain, bcast, egress, intranet). The
  target is the game's: brain's watchdog and runs handle it;
- the encoder (bcast's journal, a report every 30 s): silent, or behind
  real time (speed below SLOW);
- the host's disk and Incus's storage pool: nearly full;
- CPU steal (/proc/stat): the VPS's neighbours taking our CPU;
- the backups (backup.py's /var/lib/chat-uses/backup.json, once there is
  one): the last one failed, none worked for 36 hours, no restore drill for
  3 months.

Standard library only: it runs `incus` and `journalctl` through `incus exec`
on the host. It never reads the forwarder's journal (the stream key is in it).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VAR = Path(os.environ.get("CHAT_USES_VAR", "/var/lib/chat-uses"))
STATE = VAR / "monitor.json"
ETC = Path("/etc/chat-uses")
SETTINGS = ETC / "monitor.env"  # bootstrap.sh's monitor step writes it from host.env
WEBHOOK = ETC / "secrets" / "ops-webhook"

SLOW = 0.97  # an encoder report below this speed means the stream fell behind (as session.py)
DISK_FULL = 0.90  # the host's root disk: share used before #ops hears it
POOL_FULL = 0.85  # Incus's storage pool: share used before #ops hears it
STEAL = 0.10  # CPU steal: share of CPU time taken by the VPS's neighbours
INFRA = ("brain", "bcast", "egress", "intranet")  # containers that must run (project infra)
OPTIONAL = ("egress", "intranet")  # not deployed everywhere: missing isn't wrong (brain, bcast: it is)
GRACE = {  # seconds a problem lasts before #ops hears it
    "brain": 120,
    "container": 120,  # a redeploy replaces brain or bcast in about a minute
    "youtube": 300,
    "discord": 300,
    "encoder": 180,
    "disk": 0,
    "pool": 0,
    "steal": 300,
    "backup": 0,
    "drill": 0,
}
BACKUP_LATE = 36 * 3600.0  # seconds without a backup that worked (they run every night)
DRILL_EVERY = 92 * 86400.0  # a restore drill a quarter (PLAN.md 10.3)
CLEAR = 120  # seconds a told problem must be gone before "back to normal": no flapping
YOUTUBE_BAD = ("looking", "stopped", "ended", "reconnecting")
DISCORD_BAD = ("stopped", "reconnecting", "connecting")
_REPORT = re.compile(r"speed=([\d.]+)x")


# --- what's wrong (tested in test_monitor.py) --------------------------------------------


def problems(
    health: dict[str, Any] | None,
    running: dict[str, bool],
    encoder: list[str] | None,
    disk: float | None,
    pool: float | None,
    steal: float | None,
) -> dict[str, str]:
    """Problems now: key → what #ops reads. A key's first word picks its grace time.

    health: brain's /healthz (None: no answer). running: container → running.
    encoder: the encoder's journal lines of the last 2 minutes (None: unreadable).
    disk, pool: share used. steal: share of CPU time stolen since the last round."""
    found: dict[str, str] = {}
    if health is None:
        if running.get("brain", True):  # its container stopped: told below
            found["brain"] = "brain doesn't answer (/healthz): no chat, no overlay, no alerts from it"
    else:
        chat = health.get("chat") or {}
        youtube = chat.get("youtube") or {}
        if youtube.get("state") in YOUTUBE_BAD:
            why = youtube.get("reason") or (youtube.get("recent_errors") or ["?"])[-1]
            found["youtube"] = f"YouTube chat is {youtube['state']}: {why}"
        discord = chat.get("discord") or {}
        if discord.get("state") in DISCORD_BAD:
            found["discord"] = f"Discord is {discord['state']}: {discord.get('reason') or '?'}"
    for name, up in running.items():
        if not up:
            found[f"container {name}"] = f"the {name} container isn't running"
    if encoder is not None:
        speeds = [float(m[1]) for line in encoder if (m := _REPORT.search(line))]
        if not speeds:
            found["encoder silent"] = "the encoder hasn't reported for 2 minutes: is the stream running?"
        elif speeds[-1] < SLOW:
            found["encoder slow"] = f"the encoder is behind real time (speed {speeds[-1]}x)"
    if disk is not None and disk >= DISK_FULL:
        found["disk"] = f"the host's disk is {disk:.0%} full"
    if pool is not None and pool >= POOL_FULL:
        found["pool"] = f"Incus's storage pool is {pool:.0%} full"
    if steal is not None and steal >= STEAL:
        found["steal"] = f"CPU steal is {steal:.0%}: the VPS's neighbours are taking our CPU"
    return found


def grace(key: str) -> int:
    return GRACE.get(key.split()[0], 60)


def backup_problems(state: dict[str, Any], now: float) -> dict[str, str]:
    """backup.py's record: key → what #ops reads. Nothing until backups are set up."""
    if not state:
        return {}
    found: dict[str, str] = {}
    if error := state.get("error"):
        found["backup failed"] = f"the last backup had a problem: {str(error)[:300]}"
    last_ok = state.get("last_ok")
    if not isinstance(last_ok, (int, float)):
        found["backup none"] = "no backup has worked yet: journalctl -u chat-uses-backup"
    elif now - last_ok > BACKUP_LATE:
        found["backup late"] = f"no backup has worked for {(now - last_ok) / 3600:.0f} h"
    if isinstance(last_ok, (int, float)):
        drill = state.get("drill")
        if not isinstance(drill, (int, float)):
            found["drill"] = "no restore drill done yet: sudo chat-uses-backup --drill"
        elif now - drill > DRILL_EVERY:
            found["drill"] = (
                f"no restore drill for {(now - drill) / 86400:.0f} days: sudo chat-uses-backup --drill"
            )
        elif state.get("drill_ok") is False:
            found["drill"] = "the last restore drill failed: sudo chat-uses-backup --drill (it says why)"
    return found


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def advance(
    state: dict[str, Any], now_problems: dict[str, str], now: float
) -> tuple[list[tuple[str | None, str]], dict[str, Any]]:
    """What to post, and the state to keep. state["problems"]: key → {since, text, told,
    gone (since when a told one is gone)}; state["snooze_until"]: nothing new is told
    before then. A post is (the problem's key when it tells one, None for "back to
    normal"; the text)."""
    known: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (state.get("problems") or {}).items()}
    quiet = now < (state.get("snooze_until") or 0)
    posts: list[tuple[str | None, str]] = []
    for key, text in now_problems.items():
        entry = known.get(key) or {"since": now, "told": False}
        entry["text"] = text
        entry.pop("gone", None)  # back before CLEAR: the same problem
        if not entry["told"] and not quiet and now - entry["since"] >= grace(key):
            posts.append((key, f"🚨 {text}"))
            entry["told"] = True
        known[key] = entry
    for key in [k for k in known if k not in now_problems]:
        entry = known[key]
        if not entry.get("told"):
            del known[key]  # shorter than its grace: never told
            continue
        gone = entry.setdefault("gone", now)
        if now - gone >= CLEAR:
            del known[key]
            minutes = max(1, round((gone - entry["since"]) / 60))
            posts.append((None, f"✅ Back to normal after {minutes} min: {entry['text']}"))
    return posts, {**state, "problems": known}


def steal_share(before: list[int] | None, after: list[int]) -> float | None:
    """Share of CPU time stolen between two /proc/stat readings ([steal, total])."""
    if not before or after[1] <= before[1]:
        return None
    return (after[0] - before[0]) / (after[1] - before[1])


# --- the host ----------------------------------------------------------------------------


def run(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """A command's output; a command that hangs (Incus stuck) fails instead (exit 124)."""
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"{args[0]}: timed out")


def config(*names: str) -> dict[str, str]:
    """Settings from host.env (with the example's defaults), as the bash scripts read them.
    Installed (/usr/local/sbin/chat-uses-monitor), away from lib.sh: from monitor.env."""
    if not (HERE / "lib.sh").exists():
        try:
            lines = SETTINGS.read_text().splitlines()
        except OSError:
            return {}
        found = dict(line.split("=", 1) for line in lines if "=" in line)
        return {name: found[name] for name in names if name in found}
    values = " ".join(f'"${name}"' for name in names)
    script = f'source "{HERE}/lib.sh"; load_config; printf "%s\\0" {values}'
    return dict(zip(names, run("bash", "-c", script).stdout.split("\0"), strict=False))


def health(ip: str, port: str) -> dict[str, Any] | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # straight to brain
        with opener.open(f"http://{ip}:{port}/healthz", timeout=5) as answer:
            data = json.loads(answer.read(1_000_000))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def running(names: tuple[str, ...], project: str) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for name in names:
        answer = run("incus", "query", f"/1.0/instances/{name}/state?project={project}")
        if answer.returncode != 0 and "not found" in answer.stderr.lower() and name in OPTIONAL:
            continue  # not deployed here
        try:
            states[name] = json.loads(answer.stdout).get("status") == "Running"
        except ValueError:
            states[name] = False
    return states


def encoder_journal() -> list[str] | None:
    answer = run(
        "incus", "exec", "bcast", "--project", "infra", "--", "journalctl", "-u", "chat-bcast-encoder",
        "--since", f"@{int(time.time()) - 120}", "-o", "cat", "--no-pager",
    )  # fmt: skip
    return answer.stdout.splitlines() if answer.returncode == 0 else None


def disk_used(path: str = "/") -> float | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.used / usage.total if usage.total else None


def pool_used(pool: str) -> float | None:
    answer = run("incus", "query", f"/1.0/storage-pools/{pool}/resources")
    try:
        space = json.loads(answer.stdout)["space"]
        return space["used"] / space["total"] if space["total"] else None
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        return None


def cpu_stat() -> list[int]:
    """[steal, total] jiffies of all CPUs, from /proc/stat."""
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    return [fields[7] if len(fields) > 7 else 0, sum(fields[:8])]


def post(text: str) -> bool:
    """Into #ops through the webhook. False: not sent (no webhook, or Discord refused)."""
    try:
        url = WEBHOOK.read_text().strip()
    except OSError:
        print(f"monitor: no webhook in {WEBHOOK}: {text}", file=sys.stderr)
        return False
    body = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(
        url, body, {"Content-Type": "application/json", "User-Agent": "chat-uses-monitor/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return True
    except (OSError, urllib.error.HTTPError) as err:  # the URL is a secret: never print it
        print(f"monitor: Discord refused the post ({type(err).__name__}): {text}", file=sys.stderr)
        return False


def look(state: dict[str, Any]) -> tuple[dict[str, str], list[int]]:
    stat = cpu_stat()
    host = config("BRAIN_IP", "BRAIN_PORT", "POOL_NAME")
    infra = running(INFRA, "infra")
    found = problems(
        health(host.get("BRAIN_IP", ""), host.get("BRAIN_PORT", "")),
        infra,
        encoder_journal() if infra.get("bcast") else None,
        disk_used(),
        pool_used(host.get("POOL_NAME") or "chat"),
        steal_share(state.get("stat"), stat),
    )
    found |= backup_problems(read_json(VAR / "backup.json"), time.time())
    return found, stat


def load() -> dict[str, Any]:
    return read_json(STATE)


def save(state: dict[str, Any]) -> None:
    VAR.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(STATE)


def round_() -> int:
    state = load()
    found, stat = look(state)
    posts, state = advance(state, found, time.time())
    for key, text in posts:
        if not post(text) and key is not None:  # told again next round
            state["problems"][key]["told"] = False
    state["stat"] = stat
    save(state)
    return 0


def check() -> int:
    state = load()
    found, _stat = look(state)
    told = state.get("problems") or {}
    if not found:
        print("monitor: all good")
    for key, text in found.items():
        mark = "told" if (told.get(key) or {}).get("told") else f"told after {grace(key)} s"
        print(f"monitor: {text} ({mark})")
    snooze = (state.get("snooze_until") or 0) - time.time()
    if snooze > 0:
        print(f"monitor: snoozed for {round(snooze / 60)} more min")
    print(f"monitor: webhook {'set' if WEBHOOK.exists() else f'missing ({WEBHOOK})'}")
    return 0


def snooze(minutes: int) -> int:
    state = load()
    state["snooze_until"] = time.time() + 60 * minutes if minutes > 0 else 0
    save(state)
    print(f"monitor: nothing new told for {minutes} min" if minutes > 0 else "monitor: snooze over")
    return 0


def main(argv: list[str]) -> int:
    match argv:
        case []:
            return round_()
        case ["--check"]:
            return check()
        case ["--test"]:
            return 0 if post("👋 chat-uses monitor: alerts from the host reach #ops.") else 1
        case ["--snooze", minutes] if minutes.isdigit():
            return snooze(int(minutes))
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
