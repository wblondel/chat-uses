"""Runs (PLAN.md 6.6, 6.7): each life of the machine.

**Lives.** A run starts with `lives_start` lives (a law, 3). Every rewind
and load chat votes costs one, so does a revive, and some achievements give
one back. With none left, chat can't rewind, and the next death ends the
run. A ruleset without `lives` keeps the deaths and the runs, and lives
cost nothing.

**Death.** The watchdog says what's fatal (Watchdog.fatal: the machine off,
its system destroyed, its display or VNC dead, its screen frozen). It's a
death once it has lasted DEATH_AFTER seconds, but never while a moderator
has the stream covered or the machine is travelling or restarting, and not
in the GRACE seconds after a revive or a new run, while it boots. The run
is then dying: a death vote opens for DEATH_VOTE seconds, `!revive` against
`!rip`, among the people who were around when it died (as for power votes,
PLAN.md 6.5). Revive wins ties and silence while a life is left: brain puts
the machine back to the last snapshot from before it fell ill, for a life.
`!rip`, or no lives: the run is dead.

**The end.** A dead run (or one chat abandons with `!newrun`) gets its death
card on stream and in #graveyard (brain): the cause, how long it lived, the
lives used, its last commands, and the final blow, the last command that
ran in the minute before the machine was last seen well (a joke, not an
accusation). Then a new run, its number one higher.

The state of a run (alive, dying, ending) lives in memory: a brain restart
finds the run alive, and a machine still dead dies again a minute later.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from chatuses.identity import Presence
from chatuses.referee.laws import Laws
from chatuses.referee.votes import NOT_ELIGIBLE, Reply
from chatuses.store import Store

DEATH_AFTER = 60.0  # seconds a fatal problem must last
DEATH_VOTE = 60.0  # seconds of !revive / !rip
GRACE = 180.0  # seconds after a revive or a new run before a death can be noticed
FINAL_BLOW = 60.0  # seconds before the machine was last seen well: whose command was the last
LAST_COMMANDS = 10  # on the death card

State = Literal["alive", "dying", "ending"]
Verdict = Literal["revive", "rip"]


@dataclass
class Death:
    cause: str
    since: float  # when the machine was last seen well
    vote_ends: float | None  # None: no vote (no lives left)
    eligible: frozenset[str] = frozenset()
    ballots: dict[str, bool] = field(default_factory=dict[str, bool])  # user: revive?

    @property
    def revive(self) -> int:
        return sum(self.ballots.values())

    @property
    def rip(self) -> int:
        return len(self.ballots) - self.revive


class Runs:
    def __init__(
        self,
        store: Store,
        laws: Callable[[], Laws],
        presence: Presence,
        lives_on: Callable[[], bool] = lambda: True,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.laws = laws
        self.presence = presence
        self.lives_on = lives_on  # the ruleset has lives
        self.on_change = on_change  # the overlay's run panel
        self.clock = clock
        self.state: State = "alive"
        self.death: Death | None = None
        self.grace_until = 0.0
        self.last_well = clock()
        self._ill_since: float | None = None

    # --- the run ---------------------------------------------------------------------

    @property
    def current(self) -> dict[str, Any] | None:
        return self.store.current_run()

    def ensure(self, ruleset: str, instance: str) -> dict[str, Any]:
        """The run going on, or run #1 if there's none (brain's first start)."""
        return self.current or self.start(ruleset, instance)

    def start(self, ruleset: str, instance: str) -> dict[str, Any]:
        now = self.clock()
        run = self.store.open_run(now, ruleset, instance, self.laws().lives_start)
        self.state, self.death, self._ill_since = "alive", None, None
        self.grace_until, self.last_well = now + GRACE, now
        self.on_change()
        return run

    def lives_left(self) -> int | None:
        """None: lives are off in this ruleset (unlimited)."""
        run = self.current
        if run is None or not self.lives_on():
            return None
        return max(0, run["lives_start"] + run["lives_bonus"] - run["lives_used"])

    def spend_life(self, why: str) -> bool:
        """A rewind, a load or a revive happened: one life less. False if there was none."""
        run, left = self.current, self.lives_left()
        if run is None or left is None:
            return True
        if left == 0:
            return False
        self.store.run_lives(run["id"], self.clock(), run["lives_used"] + 1, run["lives_bonus"], why)
        self.on_change()
        return True

    def give_life(self, why: str) -> None:
        if (run := self.current) is not None:
            self.store.run_lives(run["id"], self.clock(), run["lives_used"], run["lives_bonus"] + 1, why)
            self.on_change()

    # --- death -----------------------------------------------------------------------

    def observe(self, cause: str | None, now: float, quiet: bool) -> Death | None:
        """The watchdog's verdict (Watchdog.fatal), now. A new Death when the machine has died.

        quiet: a moderator has the stream covered, or the machine is travelling or
        restarting: nothing counts."""
        if self.state != "alive" or quiet or now < self.grace_until:
            self._ill_since = None
            return None
        if cause is None:
            self._ill_since, self.last_well = None, now
            return None
        if self._ill_since is None:
            self._ill_since = now
            return None
        if now - self._ill_since < DEATH_AFTER:
            return None
        self.state = "dying"
        left = self.lives_left()
        if left == 0:
            self.death = Death(cause, self.last_well, None)
        else:
            active = self.presence.active(now)
            eligible = frozenset(u for u in active if self.presence.is_eligible(u, now))
            self.death = Death(cause, self.last_well, now + DEATH_VOTE, eligible)
        self.on_change()
        return self.death

    def ballot(self, user: str, revive: bool, now: float) -> Reply:
        death = self.death
        if self.state != "dying" or death is None or death.vote_ends is None:
            return Reply(False, "the machine isn't dying: nothing to !revive or !rip")
        if user not in death.eligible:
            return Reply(False, NOT_ELIGIBLE)
        death.ballots[user] = revive
        self.on_change()
        return Reply(True, "revive" if revive else "rip")

    def verdict(self, now: float) -> Verdict | None:
        """The death vote's outcome once it's over (or at once with no lives); None meanwhile."""
        death = self.death
        if self.state != "dying" or death is None:
            return None
        if death.vote_ends is not None and now < death.vote_ends:
            return None
        self.state = "ending"
        if death.vote_ends is None or death.rip > death.revive:
            return "rip"
        return "revive"

    def revived(self) -> None:
        """Brain brought the machine back: alive again, with time to boot."""
        now = self.clock()
        self.state, self.death, self._ill_since = "alive", None, None
        self.grace_until, self.last_well = now + GRACE, now
        self.on_change()

    def card(self, cause: str, last_well: float | None = None) -> dict[str, Any]:
        """What the death card says about the current run (brain shows it, posts it, ends the run)."""
        run = self.current
        assert run is not None
        now = self.clock()
        seen = last_well if last_well is not None else now
        last = self.store.last_inputs(run["started"], now, LAST_COMMANDS)
        blow = next((c for c in last if seen - FINAL_BLOW <= c["ts"] <= seen), None)
        return {
            "number": run["number"],
            "cause": cause,
            "lived": round(now - run["started"]),
            "lives_used": run["lives_used"],
            "last_commands": last,
            "final_blow": blow,
        }

    def end(self, card: dict[str, Any], final_frame: str | None = None) -> None:
        """The run is over (dead, or abandoned): what its card said goes in the store."""
        run = self.current
        if run is None:
            return
        self.store.end_run(
            run["id"], self.clock(), card["cause"], card["final_blow"], card["last_commands"], final_frame
        )
        self.state, self.death = "ending", None
        self.on_change()

    # --- for the overlay ---------------------------------------------------------------

    def view(self, now: float) -> dict[str, Any] | None:
        run = self.current
        if run is None:
            return None
        death = self.death
        return {
            "number": run["number"],
            "lives": self.lives_left(),
            "lives_max": run["lives_start"] + run["lives_bonus"],
            "uptime": round(now - run["started"]),
            "state": self.state,
            "death": {
                "cause": death.cause,
                "ends_in": round(max(0.0, death.vote_ends - now), 1) if death.vote_ends else None,
                "revive": death.revive,
                "rip": death.rip,
            }
            if death is not None and self.state == "dying"
            else None,
        }


def uptime_text(seconds: float) -> str:
    """2 h 05 min, 3 min, 2 d 4 h: how long a run lived."""
    minutes = math.floor(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"
