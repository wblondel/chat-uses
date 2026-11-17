"""Night mode (PLAN.md 7.6): stricter laws while no moderator is around.

A moderator is around for AWAY (15 minutes) after anything they do that
brain sees: a message in YouTube chat or `#control` (the owner and
moderator roles), a slash command. `/here` on Discord, or `!here` in chat,
counts for HERE (an hour): watching without a word.

With nobody around, night falls by itself (the setting "auto", the default):

- votes need more people: each vote's quorum floor doubles and its share of
  the active users is half as much again (`stricter`, at most everyone);
- democracy only: the mode goes to democracy, `!anarchy` and `!democracy`
  are refused (a moderator's `/mode` still works: referee.py);
- commands come slower: one every 4 seconds each, 4 a second in all
  (ratelimit.NIGHT);
- nobody gets the driver's seat (brain).

The day comes back as soon as a moderator does, with the mode chat had
before. "on" keeps the night whoever is around, "off" never brings it
(chat-brain-admin night, Discord's /night); both are kept in the store, as
is when a moderator was last around, so a restart changes nothing.
"""

import json
import math
import time
from dataclasses import dataclass, replace
from typing import Any, cast

from chatuses.referee.laws import Laws
from chatuses.store import Store

AWAY = 15 * 60.0  # seconds without a moderator before night falls
HERE = 60 * 60.0  # /here, !here: around this long without a word
SETTINGS = ("auto", "on", "off")
SAVE_EVERY = 60.0  # seconds: how often a moderator's presence is written to the store
QUORUM_FLOOR = 2  # at night, each vote's quorum floor times this...
QUORUM_SHARE = 1.5  # ...and its share of the active users times this


def stricter(laws: Laws) -> Laws:
    """The night's laws: every vote needs more people."""
    tiers = {
        action: replace(
            tier,
            quorum_floor=min(100, tier.quorum_floor * QUORUM_FLOOR),
            quorum_fraction=min(1.0, round(tier.quorum_fraction * QUORUM_SHARE, 4)),
        )
        for action, tier in laws.tiers.items()
    }
    return replace(laws, tiers=tiers)


@dataclass(frozen=True)
class Change:
    on: bool  # night fell (True) or the day came back
    why: str  # a few words: "no moderator for 15 min", "Ana is here"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _object(raw: str | None) -> dict[str, Any]:
    try:
        value: object = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class Night:
    def __init__(self, store: Store, now: float) -> None:
        self.store = store
        chosen = _object(store.setting("night_mode"))
        self.setting = str(chosen.get("setting")) if chosen.get("setting") in SETTINGS else "auto"
        self.set_by = str(chosen.get("by") or "")
        seen = _object(store.setting("moderator_seen"))
        # Never seen (a new brain): as if one had just left, so the night waits AWAY. Kept,
        # so restarts don't push the night back.
        self.seen_at = _number(seen.get("at")) or now
        self.until = _number(seen.get("until")) or self.seen_at + AWAY  # around until then
        self.by = str(seen.get("by") or "")  # the last moderator seen (their name)
        if not seen:
            store.set_setting("moderator_seen", json.dumps({"at": now, "until": self.until, "by": ""}))
        self.on = False  # brain applies the first tick's answer
        self.since = now
        self._saved = now

    # --- moderators ------------------------------------------------------------------

    def saw(self, by: str, now: float, seconds: float = AWAY) -> None:
        """A moderator did something: they're around for `seconds` (HERE for /here)."""
        longer = now + seconds > self.until
        self.until = max(self.until, now + seconds)
        self.seen_at, self.by = now, by
        if (longer and seconds > AWAY) or now - self._saved >= SAVE_EVERY:
            self._saved = now
            self.store.set_setting("moderator_seen", json.dumps({"at": now, "until": self.until, "by": by}))

    def set(self, setting: str, by: str) -> tuple[bool, str]:
        """auto, on or off; what to say back. The next tick applies it."""
        if setting not in SETTINGS:
            return False, "Night mode is auto, on or off."
        if setting == self.setting:
            return False, f"Night mode is already {setting}."
        self.setting, self.set_by = setting, by
        value = None if setting == "auto" else json.dumps({"setting": setting, "by": by})
        self.store.set_setting("night_mode", value)
        return True, {
            "auto": f"Night mode: auto (night after {AWAY / 60:.0f} min without a moderator).",
            "on": "Night mode: on, whoever is around (`night auto` to go back).",
            "off": "Night mode: off, even with no moderator around (`night auto` to go back).",
        }[setting]

    # --- time --------------------------------------------------------------------------

    def wanted(self, now: float) -> bool:
        if self.setting != "auto":
            return self.setting == "on"
        return now >= self.until

    def tick(self, now: float) -> Change | None:
        """Night fell or the day came back (None: as it was)."""
        want = self.wanted(now)
        if want == self.on:
            return None
        self.on, self.since = want, now
        if self.setting != "auto":
            return Change(want, f"set {self.setting} by {self.set_by or 'an operator'}")
        if want:
            return Change(True, f"no moderator around since {_clock(self.seen_at)}")
        return Change(False, f"{self.by or 'a moderator'} is here")

    def status_line(self, now: float) -> str:
        state = f"NIGHT since {_clock(self.since)}" if self.on else "day"
        if self.setting != "auto":
            return f"Night mode: {self.setting} (by {self.set_by or '?'}): {state}"
        if self.on:
            return (
                f"Night mode: auto, {state} (no moderator since {_clock(self.seen_at)}): /here brings the day"
            )
        who = f"{self.by}, " if self.by else ""
        left = max(0.0, self.until - now)
        seen = f"{who}{_clock(self.seen_at)}"
        return f"Night mode: auto, day (a moderator seen: {seen}; night in {left / 60:.0f} min without one)"

    def view(self) -> dict[str, Any]:
        return {"on": self.on, "setting": self.setting, "since": self.since}
