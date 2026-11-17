"""Achievements (PLAN.md 6.8): what chat did with the machine, and its rewards.

Defined in `achievements/*.yaml` (the format is in achievements/starter.yaml),
read with PyYAML's safe_load and checked whole: a file with an unknown key, a
bad type or a value out of bounds is refused with the reason, and brain
starts without it.

Every CHECK_EVERY seconds while a run is alive, brain checks the ones the run
hasn't unlocked. Most checks run in the target through Incus exec, and only
their exit status counts: nothing the hostile target says is read. Chat can
fake some of them, and that's fine. The others read brain's own state: the
screen's last OCR read, the run's time since its last rewind, its saves.

An unlocked achievement credits everyone who sent a command in the CREDIT
seconds before it; each of them gets its points, and the run its lives.
"""

import asyncio
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CHECK_EVERY = 30.0  # seconds between rounds of checks
CHECK_TIMEOUT = 10.0  # seconds one check in the target may take
CREDIT = 600.0  # seconds before an unlock in which a command earns credit
MAX_FILE = 64 * 1024
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
CHECKS: dict[str, dict[str, type]] = {  # each check type and its fields
    "process": {"pattern": str},
    "file_exists": {"path": str},
    "package": {"name": str},
    "http": {},
    "command": {"run": str},
    "ocr_text": {"pattern": str},
    "uptime_since_rewind": {"hours": float},
    "saves": {"count": int},
}
IN_TARGET = ("process", "file_exists", "package", "http", "command")


class AchievementError(ValueError):
    pass


@dataclass(frozen=True)
class Achievement:
    key: str
    name: str
    description: str
    check: dict[str, Any]
    points: int = 0
    lives: int = 0

    @property
    def kind(self) -> str:
        return str(self.check["type"])

    def command(self) -> list[str] | None:
        """What runs in the target for this check (None: brain checks it itself)."""
        c = self.check
        match c["type"]:
            case "process":
                return ["pgrep", "--", c["pattern"]]
            case "file_exists":
                return ["test", "-e", c["path"]]
            case "package":
                name = shlex.quote(c["name"])
                return [
                    "sh",
                    "-c",
                    f"dpkg-query -W -f='${{Status}}' {name} 2>/dev/null | grep -q 'ok installed'",
                ]
            case "http":
                return ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/80"]
            case "command":
                return ["sh", "-c", c["run"]]
            case _:
                return None


def _parse(entry: object, where: str) -> Achievement:
    if not isinstance(entry, dict):
        raise AchievementError(f"{where}: an achievement is a mapping")
    data: dict[str, Any] = {str(k): v for k, v in entry.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    unknown = set(data) - {"key", "name", "description", "check", "reward"}
    if unknown:
        raise AchievementError(f"{where}: unknown keys {sorted(unknown)}")
    key = data.get("key")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise AchievementError(f"{where}: key must be lowercase letters, digits and _ (32 at most)")
    where = f"{where} ({key})"
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not 1 <= len(name) <= 40:
        raise AchievementError(f"{where}: name: 1 to 40 characters")
    if not isinstance(description, str) or not 1 <= len(description) <= 120:
        raise AchievementError(f"{where}: description: 1 to 120 characters")
    check = data.get("check")
    if not isinstance(check, dict):
        raise AchievementError(f"{where}: check: a mapping with a type")
    fields: dict[str, Any] = {str(k): v for k, v in check.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    kind = fields.pop("type", None)
    if kind not in CHECKS:
        raise AchievementError(f"{where}: check type must be one of {sorted(CHECKS)}")
    wanted = CHECKS[str(kind)]
    if set(fields) != set(wanted):
        raise AchievementError(f"{where}: a {kind} check takes {sorted(wanted) or 'nothing'}")
    for field, typ in wanted.items():
        value = fields[field]
        if typ is float and isinstance(value, int) and not isinstance(value, bool):
            value = fields[field] = float(value)
        if not isinstance(value, typ) or isinstance(value, bool):
            raise AchievementError(f"{where}: {field} must be a {typ.__name__}")
        if isinstance(value, str):
            if not 1 <= len(value) <= 500:
                raise AchievementError(f"{where}: {field}: 1 to 500 characters")
            if field == "pattern":
                try:
                    re.compile(value)
                except re.error as err:
                    raise AchievementError(f"{where}: pattern: {err}") from err
        elif isinstance(value, (int, float)) and not 0 < value <= 1000:
            raise AchievementError(f"{where}: {field} must be above 0, at most 1000")
    given: object = data.get("reward") or {}
    if not isinstance(given, dict):
        raise AchievementError(f"{where}: reward takes points and lives")
    reward: dict[str, Any] = {str(k): v for k, v in given.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if set(reward) - {"points", "lives"}:
        raise AchievementError(f"{where}: reward takes points and lives")
    points, lives = reward.get("points", 0), reward.get("lives", 0)
    if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= 1000:
        raise AchievementError(f"{where}: reward points: 0 to 1000")
    if not isinstance(lives, int) or isinstance(lives, bool) or not 0 <= lives <= 2:
        raise AchievementError(f"{where}: reward lives: 0 to 2")
    return Achievement(key, name, description, {"type": kind, **fields}, points, lives)


def parse(text: str, where: str = "achievements") -> list[Achievement]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise AchievementError(f"{where}: not YAML ({err})") from err
    if not isinstance(data, list):
        raise AchievementError(f"{where}: a list of achievements")
    found = [_parse(entry, f"{where}[{i}]") for i, entry in enumerate(data)]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    keys = [a.key for a in found]
    if len(set(keys)) != len(keys):
        raise AchievementError(f"{where}: the same key twice")
    return found


def load(directory: Path) -> list[Achievement]:
    """Every achievements/*.yaml, in name order; the same key in two files is refused."""
    found: list[Achievement] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.stat().st_size > MAX_FILE:
            raise AchievementError(f"{path.name}: larger than {MAX_FILE} bytes")
        found += parse(path.read_text(encoding="utf-8"), path.name)
    keys = [a.key for a in found]
    if len(set(keys)) != len(keys):
        raise AchievementError("the same key in two files")
    return found


@dataclass
class Context:
    """What checks look at: brain fills it in."""

    exec: Callable[[list[str]], Awaitable[int]]  # in the target: its exit status
    screen_text: Callable[[], str]  # screen OCR's last read ("" if none)
    since_rewind: Callable[[], float]  # seconds since the run started or last went back
    saves: Callable[[], int]  # named saves


async def passes(achievement: Achievement, ctx: Context) -> bool:
    """Is it unlocked now? A check that can't run (the target down, a timeout) is a no."""
    c = achievement.check
    match c["type"]:
        case "ocr_text":
            return re.search(c["pattern"], ctx.screen_text(), re.IGNORECASE) is not None
        case "uptime_since_rewind":
            return ctx.since_rewind() >= c["hours"] * 3600
        case "saves":
            return ctx.saves() >= c["count"]
        case _:
            command = achievement.command()
            if command is None:
                return False
            try:
                return await asyncio.wait_for(ctx.exec(command), CHECK_TIMEOUT) == 0
            except Exception as err:  # the target is down, refuses, or hangs: not unlocked
                log.debug("achievement %s: %s", achievement.key, err)
                return False
