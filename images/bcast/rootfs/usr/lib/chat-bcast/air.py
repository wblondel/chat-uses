#!/usr/bin/python3
"""On air or off: whether the stream goes to YouTube (PLAN.md D80).

    chat-bcast-air on [BY]     YouTube gets the stream, from a few seconds on
    chat-bcast-air off [BY]    nothing goes to YouTube (YouTube ends the broadcast if
                               its auto-stop is on)
    chat-bcast-air status      which, since when, by whom

The rest of bcast runs either way: the layout, the encoder, MediaMTX (the
preview, images/bcast/check.sh). The switch is
/var/lib/chat-bcast/control/air.json. A server restart puts the stream off
air (AIR_AT_BOOT=off, the default) until someone has checked it
(infra/host/golive.sh) and puts it back; AIR_AT_BOOT=last keeps it as it was
(24/7). A dry run (FORWARD_URL) isn't YouTube: it runs on air or off.

The forwarder (forward) asks `air.py check` (exit 0: on air); the reporter
(report) imports on_air. Standard library only.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

AIR = Path(os.environ.get("AIR_FILE", "/var/lib/chat-bcast/control/air.json"))
BOOT_ID = Path(os.environ.get("AIR_BOOT_ID", "/proc/sys/kernel/random/boot_id"))  # changes when the server restarts
KEY = Path(os.environ.get("STREAM_KEY_FILE", "/etc/chat-uses/secrets/youtube-stream-key"))
SETTINGS = (Path("/etc/chat-uses/bcast.local.env"), Path("/etc/chat-uses/bcast.env"))
FORWARDER = "chat-bcast-forwarder.service"


def setting(name: str, default: str) -> str:
    """From the environment (systemd's EnvironmentFile), else bcast's env files."""
    if name in os.environ:
        return os.environ[name]
    for path in SETTINGS:
        try:
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key == name:
                    return value.strip().strip('"')
        except OSError:
            continue
    return default


def boot() -> str:
    try:
        return BOOT_ID.read_text().strip()
    except OSError:
        return ""


def read() -> dict[str, Any]:
    try:
        data = json.loads(AIR.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def on_air() -> bool:
    """Put on air, and not undone by a server restart since (unless AIR_AT_BOOT=last)."""
    state = read()
    if state.get("air") != "on":
        return False
    return setting("AIR_AT_BOOT", "off") == "last" or state.get("boot") == boot()


def set_air(on: bool, by: str) -> None:
    state = {"air": "on" if on else "off", "boot": boot(), "at": time.time(), "by": by[:80]}
    AIR.parent.mkdir(parents=True, exist_ok=True)
    part = AIR.with_name(AIR.name + ".part")
    part.write_text(json.dumps(state) + "\n")
    part.chmod(0o644)
    part.replace(AIR)


def status() -> str:
    state = read()
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["at"])) if "at" in state else "?"
    who = f", by {state['by']}" if state.get("by") else ""
    if on_air():
        return f"on air since {when}{who}"
    if state.get("air") == "on":
        return f"off air: the server restarted since it was put on air ({when}{who}; AIR_AT_BOOT=off)"
    if state:
        return f"off air since {when}{who}"
    return "off air (never put on air on this bcast)"


def _has_key() -> bool:
    try:
        return KEY.stat().st_size > 0
    except OSError:
        return False


def main(argv: list[str]) -> int:
    match argv:
        case ["check"]:
            return 0 if on_air() else 1
        case ["status"]:
            print(status())
            return 0
        case [("on" | "off") as what, *by]:
            if what == "on" and not _has_key():
                print(f"nothing to put on air: no stream key in {KEY} (images/bcast/deploy.sh pushes it)")
                return 1
            set_air(what == "on", " ".join(by) or "console")
            subprocess.run(["systemctl", "restart", FORWARDER], check=False, timeout=60)
            if what == "on":
                print("on air: YouTube gets the stream in a few seconds")
            else:
                print("off air: nothing goes to YouTube now")
            return 0
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
