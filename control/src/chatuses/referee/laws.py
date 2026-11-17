"""Rulesets and laws (PLAN.md 6.4, 6.12).

A ruleset is a YAML file in rulesets/ (the repo's; /usr/lib/chat-brain/rulesets
in brain) that sets the laws, the features and the commands chat may use.
Everything left out keeps the defaults below, which are PLAN.md's numbers.
Every law has bounds, and a ruleset with an unknown key, a value out of
bounds or the wrong type is refused as a whole: a typo must not quietly
leave a law at its default.

On top of the ruleset, the `laws` table holds laws changed while running
(an operator now; chat's `!amend` in Phase 9). Those changes are checked
against the same bounds.
"""

import math
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, cast

import yaml

MAX_FILE = 64 * 1024  # bytes: a ruleset is a page of YAML

# Power votes (6.4): the actions chat can propose, and the commands that do.
ACTIONS = ("reboot", "save", "reset", "rewind", "load", "newrun", "impeach", "macro", "unmacro")
# What a ruleset may turn on. Not all are built yet: brain says which.
FEATURES = frozenset(
    {"anarchy", "democracy", "power_votes", "snapshots", "lives", "achievements", "driver_seat", "chaos"}
)
# Chat commands a ruleset may turn off (6.1): power votes, modes, ballots.
COMMANDS = frozenset(ACTIONS) | {"anarchy", "democracy", "yes", "no", "veto"}


class RulesetError(ValueError):
    pass


@dataclass(frozen=True)
class Tier:
    """How a power vote is decided (6.4)."""

    threshold: float  # share of yes among yes + no
    quorum_fraction: float  # of the users active when it opened...
    quorum_floor: int  # ...but at least this many yes
    window_s: int  # voting time
    veto_s: int  # after passing: a countdown during which chat can veto (0: none)
    cooldown_min: float  # before the same action can be proposed again

    def quorum(self, active: int) -> int:
        return max(self.quorum_floor, math.ceil(self.quorum_fraction * active))


TIER_BOUNDS: dict[str, tuple[float, float]] = {
    "threshold": (0.5, 1.0),
    "quorum_fraction": (0.0, 1.0),
    "quorum_floor": (1, 100),
    "window_s": (10, 300),
    "veto_s": (0, 300),
    "cooldown_min": (0, 1440),
}

DEFAULT_TIERS: dict[str, Tier] = {
    "reboot": Tier(0.5, 0.15, 2, 30, 0, 2),
    "save": Tier(0.5, 0.15, 2, 30, 0, 2),
    "reset": Tier(0.6, 0.20, 3, 30, 0, 5),
    "rewind": Tier(0.66, 0.25, 3, 30, 30, 10),
    "load": Tier(0.66, 0.25, 3, 30, 30, 10),
    "newrun": Tier(0.75, 0.35, 4, 45, 60, 60),
    "impeach": Tier(0.5, 0.20, 2, 20, 0, 5),
    # Macros (6.10) shape chat's language for good: a clear majority, and no flood of them.
    "macro": Tier(0.6, 0.20, 3, 30, 0, 2),
    "unmacro": Tier(0.6, 0.20, 3, 30, 0, 2),
}


@dataclass(frozen=True)
class Laws:
    """Every number chat lives by. Bounds in LAW_BOUNDS."""

    # Votes (6.4): a passed vote is cancelled during its countdown by
    # max(veto_min, veto_fraction x yes) vetoes.
    veto_min: int = 3
    veto_fraction: float = 1 / 3
    # Modes (6.3)
    democracy_window_s: int = 12
    meter_step: int = 2
    meter_democracy_at: int = 80
    meter_anarchy_at: int = 20
    meter_push_gap_s: int = 10
    meter_drift_s: int = 10  # seconds per point back toward the centre (0: no drift)
    mode_lock_s: int = 120
    # Snapshots (6.6)
    auto_snapshot_min: int = 15
    auto_snapshot_keep: int = 8
    named_saves: int = 3
    # Later phases: runs (8), the driver's seat (9)
    lives_start: int = 3
    driver_interval_min: int = 10
    driver_duration_s: int = 60
    tiers: dict[str, Tier] = field(default_factory=lambda: dict(DEFAULT_TIERS))

    def tier(self, action: str) -> Tier:
        return self.tiers[action]


LAW_BOUNDS: dict[str, tuple[float, float]] = {
    "veto_min": (1, 50),
    "veto_fraction": (0.0, 1.0),
    "democracy_window_s": (5, 60),
    "meter_step": (1, 10),
    "meter_democracy_at": (51, 100),
    "meter_anarchy_at": (0, 49),
    "meter_push_gap_s": (1, 120),
    "meter_drift_s": (0, 120),
    "mode_lock_s": (0, 600),
    "auto_snapshot_min": (10, 30),
    "auto_snapshot_keep": (2, 20),
    "named_saves": (1, 10),
    "lives_start": (1, 9),
    "driver_interval_min": (1, 120),
    "driver_duration_s": (10, 600),
}


@dataclass(frozen=True)
class Ruleset:
    name: str
    target_profile: str = "debian-xfce"
    features: frozenset[str] = FEATURES
    disabled: frozenset[str] = frozenset()  # commands chat can't use
    laws: Laws = field(default_factory=Laws)

    def allows(self, command: str) -> bool:
        return command not in self.disabled


DEFAULT = Ruleset("roguelike")


def _number(value: object, kind: type, bounds: tuple[float, float], where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RulesetError(f"{where}: {value!r} isn't a number")
    if kind is int and (not float(value).is_integer()):
        raise RulesetError(f"{where}: {value!r} must be a whole number")
    low, high = bounds
    if not low <= value <= high:
        raise RulesetError(f"{where}: {value!r} is out of bounds ({low:g} to {high:g})")
    return int(value) if kind is int else float(value)


_LAW_TYPES = {f.name: f.type for f in fields(Laws) if f.name != "tiers"}
_TIER_TYPES = {f.name: f.type for f in fields(Tier)}


def law_value(name: str, value: object) -> float:
    """A law's value checked against its bounds; RulesetError if it can't be."""
    if name not in LAW_BOUNDS:
        raise RulesetError(f"no law called {name!r}")
    kind = int if _LAW_TYPES[name] in (int, "int") else float
    return _number(value, kind, LAW_BOUNDS[name], name)


def tier_value(action: str, name: str, value: object) -> float:
    if action not in DEFAULT_TIERS:
        raise RulesetError(f"tiers: no action called {action!r} ({', '.join(ACTIONS)})")
    if name not in TIER_BOUNDS:
        raise RulesetError(f"tiers.{action}: no setting called {name!r} ({', '.join(TIER_BOUNDS)})")
    kind = int if _TIER_TYPES[name] in (int, "int") else float
    return _number(value, kind, TIER_BOUNDS[name], f"tiers.{action}.{name}")


def apply(laws: Laws, changes: dict[str, object]) -> Laws:
    """These laws with some changed: "name" for a law, "tiers.ACTION.SETTING" for a vote's."""
    plain: dict[str, Any] = {}
    tiers = dict(laws.tiers)
    for key, value in changes.items():
        if key.startswith("tiers."):
            parts = key.split(".")
            if len(parts) != 3:
                raise RulesetError(f"{key}: write tiers.ACTION.SETTING")
            _, action, name = parts
            checked = tier_value(action, name, value)
            tiers[action] = replace(tiers[action], **{name: checked})
        else:
            plain[key] = law_value(key, value)
    # The meter's bounds (0-49, 51-100) keep anarchy's threshold below democracy's.
    return replace(laws, **plain, tiers=tiers)


def _mapping(value: object, where: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in cast(dict[object, object], value)):
        raise RulesetError(f"{where} must be a mapping")
    return cast(dict[str, object], value)


def _names(value: object, allowed: frozenset[str], where: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in cast(list[object], value)):
        raise RulesetError(f"{where} must be a list of names")
    names = frozenset(cast(list[str], value))
    if unknown := names - allowed:
        raise RulesetError(f"{where}: unknown {', '.join(sorted(unknown))} ({', '.join(sorted(allowed))})")
    return names


def parse(text: str, name: str) -> Ruleset:
    """A ruleset from its YAML; `name` is the file's (rulesets/NAME.yaml)."""
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise RulesetError(f"not valid YAML: {err}") from err
    top = _mapping(data, "the ruleset")
    if unknown := set(top) - {
        "name",
        "description",
        "target_profile",
        "features",
        "laws",
        "tiers",
        "commands",
    }:
        raise RulesetError(f"unknown keys: {', '.join(sorted(unknown))}")
    if top.get("name", name) != name:
        raise RulesetError(f"its name ({top.get('name')!r}) isn't its file's ({name!r})")
    profile = top.get("target_profile", DEFAULT.target_profile)
    if not isinstance(profile, str):
        raise RulesetError("target_profile must be a name")
    changes: dict[str, object] = dict(_mapping(top.get("laws"), "laws"))
    for action, settings in _mapping(top.get("tiers"), "tiers").items():
        for setting, value in _mapping(settings, f"tiers.{action}").items():
            changes[f"tiers.{action}.{setting}"] = value
    commands = _mapping(top.get("commands"), "commands")
    if unknown := set(commands) - {"disabled"}:
        raise RulesetError(f"commands: unknown keys {', '.join(sorted(unknown))}")
    features = _names(top["features"], FEATURES, "features") if "features" in top else DEFAULT.features
    return Ruleset(
        name=name,
        target_profile=profile,
        features=features,
        disabled=_names(commands.get("disabled"), COMMANDS, "commands.disabled"),
        laws=apply(Laws(), changes),
    )


def load(directory: Path, name: str) -> Ruleset:
    """rulesets/NAME.yaml; RulesetError if it's missing or wrong."""
    if not name or not name.replace("-", "").replace("_", "").isalnum() or len(name) > 40:
        raise RulesetError(f"{name!r} isn't a ruleset name")
    path = directory / f"{name}.yaml"
    try:
        if path.stat().st_size > MAX_FILE:
            raise RulesetError(f"{path} is too big")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise RulesetError(f"no ruleset {name!r} in {directory}") from err
    except (OSError, UnicodeDecodeError) as err:
        raise RulesetError(f"can't read {path}: {err}") from err
    return parse(text, name)


def available(directory: Path) -> list[str]:
    try:
        return sorted(p.stem for p in directory.glob("*.yaml"))
    except OSError:
        return []
