"""Moderators' tools (PLAN.md 7.5, 7.6): panic, bans and timeouts across
platforms, lockdown.

Discord's slash commands, the admin dashboard and chat-brain-admin all act
through here, so they behave the same. Every action goes in the event log
with who did it, and the state is kept in the store: a restart doesn't lift
a ban, end a panic or end a lockdown.

- **Panic** covers the stream, mutes the target and freezes input (brain's
  `Effects`), takes a forensic snapshot and alerts #mod-alerts. It lasts
  until someone resumes.
- **A content hold** is a panic started by screen OCR (screen_ocr.py, in
  enforce mode): the cover says "Content check". A moderator's /panic during
  a hold turns it into a panic; /resume ends either.
- **Bans and timeouts** are by unified ID (`yt:…`, `dc:…`), or by the name
  the overlay showed, for anyone who sent a command recently. Moderators and
  the owner can't be banned this way.
- **Ending a vote** (/endvote): a power vote that's going wrong stops at
  once (not one already being carried out).
- **Rewinding** (/rewind-to): the target goes back to one of chat's
  snapshots at once, without a vote (an open vote ends); #mod-alerts says
  when it's done.
- **Lockdown** (raids): only trusted roles (owner, moderator, Discord
  regulars, YouTube members) and people brain has known for a day may send
  commands. Points (Phase 6) will join them.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from chatuses.pipeline import LOCKED, ChatMessage
from chatuses.safety.strikes import Strikes
from chatuses.store import PERMANENT, Store, User

MODERATOR_ROLES = frozenset({"owner", "moderator"})
TRUSTED_ROLES = MODERATOR_ROLES | {"regular", "member"}
LOCKDOWN_AGE = 24 * 3600.0  # seconds: during lockdown, people known this long still play
NAME_LOOKBACK = 7 * 24 * 3600.0  # a ban by name looks among people seen this recently
DEFAULT_TIMEOUT = 10  # minutes
__all__ = ["LOCKED", "Answer", "Effects", "Moderation", "describe_duration", "parse_duration"]

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION = re.compile(r"(?:(\d{1,6})([smhdw]))+")
_PART = re.compile(r"(\d{1,6})([smhdw])")
_ID = re.compile(r"(?:yt|dc|console):[^\s:]{1,64}")
_MENTION = re.compile(r"<@!?(\d{1,20})>")
_FOREVER = ("perm", "permanent", "forever")


def parse_duration(text: str) -> float | None:
    """`30s`, `10m`, `2h`, `7d`, `1w`, `1d12h` in seconds; `perm` is forever (inf).

    None if it isn't one of those (or is zero)."""
    text = text.strip().lower().replace(" ", "")
    if text in _FOREVER:
        return float("inf")
    if not _DURATION.fullmatch(text):
        return None
    seconds = sum(int(n) * _UNITS[unit] for n, unit in _PART.findall(text))
    return float(seconds) or None


def describe_duration(seconds: float) -> str:
    if seconds == float("inf"):
        return "permanently"
    for unit, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = int(seconds // size)
            return f"for {n} {unit}{'s' if n != 1 else ''}"
    n = round(seconds)
    if n >= 60:
        return f"for {round(seconds / 60)} minutes"
    return f"for {n} second{'s' if n != 1 else ''}"


def clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


@dataclass(frozen=True)
class Answer:
    ok: bool
    text: str  # for the moderator who asked


class Effects(Protocol):
    """What moderation needs from brain."""

    # Cover and mute the stream, freeze input (or undo); kind: "panic" or "hold".
    def panic_changed(self, on: bool, kind: str = "panic") -> None: ...
    def lockdown_changed(self, on: bool) -> None: ...  # the overlay says so
    def alert(self, text: str) -> None: ...  # a post in #mod-alerts
    def snapshot(self, name: str) -> None: ...  # a forensic snapshot of the target, in the background
    def end_vote(self, why: str) -> tuple[bool, str]: ...  # ended?, what to say (referee/votes.py)
    def rewind_to(self, which: str, by: str) -> tuple[bool, str]: ...  # started?, what to say (snapshots.py)


class Moderation:
    def __init__(
        self,
        store: Store,
        effects: Effects,
        strikes: Strikes,
        safe_name: Callable[[str, str], str] = lambda _user, name: name,
    ) -> None:
        self.store = store
        self.effects = effects
        self.strikes = strikes
        self.safe_name = safe_name
        self.panic_state: dict[str, Any] | None = self._load("panic")
        self.lockdown_state: dict[str, Any] | None = self._load("lockdown")
        self.last_panic: dict[str, Any] | None = None  # the one resume just ended

    def _load(self, key: str) -> dict[str, Any] | None:
        raw = self.store.setting(key)
        try:
            value: object = json.loads(raw) if raw else None
        except ValueError:
            value = {}
        if value is None:
            return None
        if not isinstance(value, dict):  # unreadable: stay on, the safe side
            return {"since": 0.0, "by": "?", "reason": "restored"}
        return cast(dict[str, Any], value)

    def _save(self, key: str, value: dict[str, Any] | None) -> None:
        self.store.set_setting(key, json.dumps(value) if value is not None else None)

    @property
    def panicking(self) -> bool:
        return self.panic_state is not None

    @property
    def locked(self) -> bool:
        return self.lockdown_state is not None

    # --- panic -----------------------------------------------------------------

    @property
    def kind(self) -> str:
        """What covers the stream: "panic", "hold", or "" for nothing."""
        return str(self.panic_state.get("kind", "panic")) if self.panic_state is not None else ""

    def panic(self, by: str, reason: str = "", now: float | None = None) -> Answer:
        now = time.time() if now is None else now
        self.effects.panic_changed(True)  # first: the stream is covered before anything else happens
        reason = reason.strip()[:200]
        if self.panic_state is not None and self.kind == "hold":  # a moderator confirms: it's a panic
            self.panic_state = {**self.panic_state, "kind": "panic", "by": by, "reason": reason}
            self._save("panic", self.panic_state)
            self.store.event(now, "panic", {"by": by, "reason": reason, "from_hold": True})
            self.effects.alert(
                f"🚨 **Panic** by {by}{f': {reason}' if reason else ''}, after the content hold."
            )
            return Answer(True, "The content hold is now a panic. /resume to end it.")
        if self.panic_state is not None:
            since = self.panic_state.get("since", 0.0)
            return Answer(
                True, f"Panic was already on (since {clock(since)}, by {self.panic_state.get('by')})."
            )
        self.panic_state = {"since": now, "by": by, "reason": reason, "kind": "panic"}
        self._save("panic", self.panic_state)
        self.store.event(now, "panic", {"by": by, "reason": reason})
        self.effects.alert(
            f"🚨 **Panic** by {by}{f': {reason}' if reason else ''}. "
            "The stream is covered, the target's sound muted, input frozen. `/resume` when it's safe."
        )
        self.effects.snapshot("panic-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)))
        return Answer(
            True, "Panic on: the stream is covered, the sound muted, input frozen. /resume to end it."
        )

    def hold(self, by: str, lines: tuple[str, ...], now: float | None = None) -> Answer:
        """A content hold: screen OCR saw a listed word (enforce mode)."""
        now = time.time() if now is None else now
        if self.panic_state is not None:
            return Answer(False, "The stream is already covered.")
        self.effects.panic_changed(True, "hold")
        kept = [line[:200] for line in lines[:20]]
        self.panic_state = {"since": now, "by": by, "reason": "content check", "kind": "hold", "lines": kept}
        self._save("panic", self.panic_state)
        self.store.event(now, "hold", {"by": by, "lines": kept})
        self.effects.alert(
            f'🛑 **Content hold** by {by}: the stream says "Content check", the sound is muted, '
            "input frozen. `/resume` if what's on screen is fine, `/panic` if it isn't."
        )
        self.effects.snapshot("hold-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)))
        return Answer(True, "Content hold on.")

    def resume(self, by: str, now: float | None = None) -> Answer:
        now = time.time() if now is None else now
        if self.panic_state is None:
            self.effects.panic_changed(False)  # make sure: nothing should be covered
            return Answer(False, "There's no panic to end.")
        lasted = now - float(self.panic_state.get("since", now))
        kind = self.kind
        self.last_panic, self.panic_state = self.panic_state, None
        self._save("panic", None)
        self.store.event(now, "resume", {"by": by, "seconds": round(lasted), "kind": kind})
        self.effects.panic_changed(False, kind)
        cleared = (
            " What it saw won't hold the stream again while it stays on screen." if kind == "hold" else ""
        )
        self.effects.alert(f"✅ **Resumed** by {by} after {_minutes(lasted)}.{cleared}")
        return Answer(True, "Resumed: the stream is live and chat has control again.")

    # --- bans and timeouts ---------------------------------------------------------

    def ban(self, by: str, who: str, duration: str, reason: str = "", now: float | None = None) -> Answer:
        seconds = parse_duration(duration)
        if seconds is None:
            return Answer(False, "Duration: like 30m, 2h, 7d, 1w, or perm.")
        return self._ban(by, who, seconds, reason, "ban", now)

    def timeout(self, by: str, who: str, minutes: int = DEFAULT_TIMEOUT, now: float | None = None) -> Answer:
        if not 1 <= minutes <= 7 * 24 * 60:
            return Answer(False, "A timeout lasts 1 minute to 7 days (10080 minutes); longer is a /ban.")
        return self._ban(by, who, minutes * 60.0, "", "timeout", now)

    def _ban(self, by: str, who: str, seconds: float, reason: str, kind: str, now: float | None) -> Answer:
        now = time.time() if now is None else now
        found = self.resolve(who, now)
        if isinstance(found, Answer):
            return found
        user = self.store.user(found)
        if user and set(user["roles"].split()) & MODERATOR_ROLES:
            return Answer(
                False, f"{self.label(found)} is a moderator or the owner: take the role away first."
            )
        until = PERMANENT if seconds == float("inf") else now + seconds
        reason = reason.strip()[:200]
        self.store.ban(now, found, until, by, reason, kind)
        what = "banned" if kind == "ban" else "timed out"
        text = f"{self.label(found)} {what} {describe_duration(seconds)}"
        self.effects.alert(f"⛔ {text} by {by}{f': {reason}' if reason else ''}.")
        return Answer(True, f"{text}: their commands are ignored on every platform.")

    def unban(self, by: str, who: str, now: float | None = None) -> Answer:
        now = time.time() if now is None else now
        found = self.resolve(who, now)
        if isinstance(found, Answer):
            return found
        if self.store.banned_until(found) <= now:
            return Answer(False, f"{self.label(found)} isn't banned or timed out.")
        self.store.unban(now, found, by)
        self.strikes.forget(found)
        self.effects.alert(f"↩️ {self.label(found)} unbanned by {by}.")
        return Answer(True, f"{self.label(found)} may send commands again.")

    def resolve(self, who: str, now: float) -> str | Answer:
        """A unified ID, a Discord mention, or the name the overlay showed → the user's ID."""
        who = who.strip()
        if _ID.fullmatch(who):
            return who
        if mention := _MENTION.fullmatch(who):
            return f"dc:{mention[1]}"
        if not who:
            return Answer(False, "Who? Their name as the overlay shows it, or their ID (yt:… or dc:…).")
        wanted = who.removeprefix("@").casefold()
        matches = [
            u["id"]
            for u in self.store.users_seen_since(now - NAME_LOOKBACK)
            if wanted in (u["display_name"].casefold(), self.safe_name(u["id"], u["display_name"]).casefold())
        ]
        if not matches:
            return Answer(
                False,
                f"Nobody called {who!r} sent a command in the last 7 days. Use their ID (yt:… or dc:…): "
                "the admin dashboard's audit shows it.",
            )
        if len(matches) > 1:
            return Answer(False, f"Several people are called {who!r}: use an ID: {', '.join(matches[:10])}")
        return matches[0]

    def label(self, user_id: str) -> str:
        user = self.store.user(user_id)
        name = self.safe_name(user_id, user["display_name"]) if user and user["display_name"] else ""
        return f"{name} ({user_id})" if name else user_id

    # --- lockdown -------------------------------------------------------------

    def lockdown(self, by: str, on: bool, now: float | None = None) -> Answer:
        now = time.time() if now is None else now
        if on == self.locked:
            return Answer(False, f"Lockdown is already {'on' if on else 'off'}.")
        self.lockdown_state = {"since": now, "by": by} if on else None
        self._save("lockdown", self.lockdown_state)
        self.store.event(now, "lockdown", {"on": on, "by": by})
        self.effects.lockdown_changed(on)
        if on:
            self.effects.alert(f"🔒 **Lockdown** on, by {by}: only regulars can send commands.")
            return Answer(True, "Lockdown on: only regulars, members, moderators and people known for a day.")
        self.effects.alert(f"🔓 Lockdown off, by {by}.")
        return Answer(True, "Lockdown off: everyone can send commands again.")

    def held_by_lockdown(self, msg: ChatMessage) -> bool:
        """During lockdown: is this person kept out?"""
        if self.lockdown_state is None or msg.roles & TRUSTED_ROLES:
            return False
        user = self.store.user(msg.user_id)
        first_seen = user["first_seen"] if user else msg.ts
        return msg.ts - first_seen < LOCKDOWN_AGE

    # --- votes ------------------------------------------------------------------------

    def endvote(self, by: str, now: float | None = None) -> Answer:
        now = time.time() if now is None else now
        ended, text = self.effects.end_vote("ended by a moderator")
        if ended:
            self.store.event(now, "endvote", {"by": by})
        return Answer(ended, text if ended else "No vote is running.")

    def rewind_to(self, by: str, which: str, now: float | None = None) -> Answer:
        now = time.time() if now is None else now
        started, text = self.effects.rewind_to(which.strip()[:40], by)
        if started:
            self.store.event(now, "rewind-to", {"by": by, "snapshot": which.strip()[:40]})
        return Answer(started, text)

    # --- for the dashboard and /status -----------------------------------------------

    def state(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "panic": self.panic_state,
            "lockdown": self.lockdown_state,
            "banned": [_ban_row(u, self.safe_name) for u in self.store.banned(now)],
        }


def _ban_row(user: User, safe_name: Callable[[str, str], str]) -> dict[str, Any]:
    until = user["banned_until"]
    return {
        "user": user["id"],
        "name": safe_name(user["id"], user["display_name"]) if user["display_name"] else "",
        "until": None if until >= PERMANENT else until,
    }


def _minutes(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} seconds"
    return f"{round(seconds / 60)} minutes"
