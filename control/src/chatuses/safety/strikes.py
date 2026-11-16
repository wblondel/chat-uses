"""Strikes and automatic timeouts (PLAN.md 7.4): 3 refused slurs in 10 minutes
earn a 10-minute timeout. Personal details don't count: they're often
innocent (a phone number typed by mistake), and they're refused anyway."""

from collections import deque
from dataclasses import dataclass, field

LIMIT = 3
WINDOW = 600.0  # seconds
TIMEOUT = 600.0  # seconds


@dataclass
class Strikes:
    limit: int = LIMIT
    window: float = WINDOW
    timeout: float = TIMEOUT
    _hits: dict[str, deque[float]] = field(default_factory=dict[str, deque[float]])

    def add(self, user: str, now: float) -> float | None:
        """Count a strike; returns when the user's timeout ends if this one earns it."""
        hits = self._hits.setdefault(user, deque())
        hits.append(now)
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(self._hits) > 10_000:  # forget users with nothing recent
            self._hits = {u: h for u, h in self._hits.items() if h and now - h[-1] <= self.window}
        if len(hits) < self.limit:
            return None
        hits.clear()
        return now + self.timeout

    def forget(self, user: str) -> None:
        """A moderator lifted the user's timeout: start counting again from zero."""
        self._hits.pop(user, None)
