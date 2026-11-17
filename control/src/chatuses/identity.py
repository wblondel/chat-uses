"""Who is who, and who is around (PLAN.md 6.5).

Users from every platform share one ID space: "yt:<channel id>",
"dc:<user id>". Presence is per stream session (a new Presence per session):

- active at t: sent any message in the 5 minutes up to t, and not banned;
- eligible for a vote opened at T: active at T and present in this session
  since at least T - 2 min, so a raid of brand-new accounts can't swing a
  vote straight away.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Platform = Literal["yt", "dc", "console"]  # console: brain's own console chat (console.py)

ACTIVE_WINDOW = 300.0  # seconds
SETTLE_TIME = 120.0  # seconds present before a vote counts


def user_id(platform: Platform, platform_id: str) -> str:
    if not platform_id or not platform_id.isprintable() or ":" in platform_id or len(platform_id) > 64:
        raise ValueError("bad platform user id")
    return f"{platform}:{platform_id}"


@dataclass
class Presence:
    is_banned: Callable[[str], bool] = lambda _user: False
    _first_seen: dict[str, float] = field(default_factory=dict[str, float])
    _last_seen: dict[str, float] = field(default_factory=dict[str, float])

    def saw(self, user: str, now: float) -> None:
        self._first_seen.setdefault(user, now)
        self._last_seen[user] = now

    def is_active(self, user: str, now: float) -> bool:
        last = self._last_seen.get(user)
        return last is not None and now - ACTIVE_WINDOW <= last <= now and not self.is_banned(user)

    def active(self, now: float) -> set[str]:
        return {u for u in self._last_seen if self.is_active(u, now)}

    def seen_since(self, start: float, now: float) -> set[str]:
        """Who sent a message between start and now (and isn't banned): a minute's points."""
        return {u for u, last in self._last_seen.items() if start <= last <= now and not self.is_banned(u)}

    def is_eligible(self, user: str, vote_opened: float) -> bool:
        first = self._first_seen.get(user)
        return first is not None and first <= vote_opened - SETTLE_TIME and self.is_active(user, vote_opened)

    def forget_idle(self, now: float) -> None:
        """Stop scanning users idle longer than the active window. Their first
        sighting stays for the session: someone who comes back after a break
        isn't a new arrival."""
        for user in [u for u, last in self._last_seen.items() if now - last > ACTIVE_WINDOW]:
            del self._last_seen[user]
