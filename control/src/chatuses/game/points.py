"""Points (PLAN.md 6.11): what playing earns, for the leaderboard and `/me`.

- +1 for each minute someone is active in chat (a message in it; 1 a minute);
- +2 for each of their commands that ran on the machine, at most
  COMMAND_CAP a minute (spam earns nothing more);
- an achievement's points, to each person it credits (achievements.py).

Never negative, nothing is taken away. Banned people earn nothing. Kept on
each user in the store, so a deletion request takes them with the user.
"""

import time
from collections.abc import Callable

from chatuses.identity import Presence
from chatuses.store import Store

ACTIVE = 1  # points a minute for being active
COMMAND = 2  # points a command that ran
COMMAND_CAP = 10  # command points a minute, at most


class Points:
    def __init__(self, store: Store, presence: Presence, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.presence = presence
        self.clock = clock
        self._minute: dict[str, tuple[int, int]] = {}  # user -> (minute, command points in it)

    def command_ran(self, user: str, now: float | None = None) -> int:
        """A command of theirs ran: its points (0 past the minute's cap)."""
        now = self.clock() if now is None else now
        minute = int(now // 60)
        seen, earned = self._minute.get(user, (minute, 0))
        if seen != minute:
            earned = 0
        add = min(COMMAND, COMMAND_CAP - earned)
        if add <= 0:
            return 0
        self.store.add_points([user], add)
        self._minute[user] = (minute, earned + add)
        if len(self._minute) > 10_000:  # forget last minutes'
            self._minute = {u: v for u, v in self._minute.items() if v[0] == minute}
        return add

    def active_minute(self, now: float | None = None) -> int:
        """The minute is over: everyone who was active in it gets a point. How many did."""
        now = self.clock() if now is None else now
        active = sorted(self.presence.seen_since(now - 60, now))
        self.store.add_points(active, ACTIVE)
        return len(active)
