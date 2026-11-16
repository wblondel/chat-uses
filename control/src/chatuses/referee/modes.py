"""Anarchy and democracy (PLAN.md 6.3).

- **Anarchy:** every valid input runs, in arrival order (the executor's).
- **Democracy:** input commands are ballots. A window opens with the first
  ballot and lasts `democracy_window_s` (12 s); identical commands (as
  commands.describe writes them) are tallied, each person's latest ballot
  counting once, and the winner runs. Ties go to the earliest.
- **The meter,** 0 to 100 from 50: `!anarchy` pushes it down, `!democracy`
  up, by `meter_step`, once per person every `meter_push_gap_s`. At
  `meter_democracy_at` (80) the mode becomes democracy, at
  `meter_anarchy_at` (20) anarchy again (hysteresis); after a switch the
  mode stays put for `mode_lock_s` (2 minutes). The meter drifts back
  toward the centre, a point every `meter_drift_s`.

Pushing the meter changes how everyone plays, so like voting (6.5) it takes
people who've been around for 2 minutes: a raid can't flip the mode.
Ballots in democracy are ordinary input, open to anyone whose command was
accepted (lockdown handles raids there).

Driven by `now`: brain calls tick() a few times a second; tests use a fake
clock. What the winner does is brain's (on_winner).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from chatuses.commands import InputCommand, describe
from chatuses.identity import Presence
from chatuses.referee.laws import Laws
from chatuses.referee.votes import Reply

Mode = Literal["anarchy", "democracy"]
CENTRE = 50.0
TALLY_SHOWN = 4  # options the overlay shows


@dataclass(frozen=True)
class Ballot:
    user: str
    name: str
    command: InputCommand
    text: str  # describe(command): what's counted
    at: float


@dataclass(frozen=True)
class Winner:
    text: str
    votes: int
    total: int  # ballots cast in the window
    ballot: Ballot  # the earliest ballot for it: who and what runs


class Modes:
    def __init__(
        self,
        presence: Presence,
        laws: Callable[[], Laws],
        on_change: Callable[[], None] = lambda: None,
        on_winner: Callable[[Winner], None] = lambda _winner: None,
        now: float = 0.0,
    ) -> None:
        self.presence = presence
        self.laws = laws
        self.on_change = on_change  # the overlay: meter, mode, tally
        self.on_winner = on_winner  # brain runs it
        self.mode: Mode = "anarchy"
        self.meter = CENTRE
        self.locked_until = 0.0
        self.window_ends: float | None = None
        self.ballots: dict[str, Ballot] = {}  # this window's, by user
        self._last_push: dict[str, float] = {}
        self._drift_at = now

    # --- chat ------------------------------------------------------------------------

    def push(self, user: str, toward: Mode, now: float) -> Reply:
        laws = self.laws()
        if not self.presence.is_eligible(user, now):
            return Reply(False, "you can push the meter once you've been here 2 minutes")
        last = self._last_push.get(user)
        if last is not None and now - last < laws.meter_push_gap_s:
            return Reply(False, f"one push every {laws.meter_push_gap_s} s")
        self._last_push[user] = now
        step = laws.meter_step if toward == "democracy" else -laws.meter_step
        self.meter = min(100.0, max(0.0, self.meter + step))
        self._switch_if_due(now)
        self.on_change()
        return Reply(True, toward)

    def ballot(self, user: str, name: str, command: InputCommand, now: float) -> Reply:
        """A democracy ballot (the caller checked the mode)."""
        if self.window_ends is None:
            self.window_ends = now + self.laws().democracy_window_s
        self.ballots[user] = Ballot(user, name, command, describe(command), now)
        self.on_change()
        return Reply(True, "counted")

    # --- time ------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        changed = self._drift(now)
        changed = self._switch_if_due(now) or changed
        if self.window_ends is not None and now >= self.window_ends:
            winner = self.winner()
            self.window_ends, self.ballots = None, {}
            changed = True
            if winner is not None:
                self.on_winner(winner)
        if changed:
            self.on_change()

    def _drift(self, now: float) -> bool:
        every = self.laws().meter_drift_s
        if every <= 0:
            self._drift_at = now
            return False
        steps = int((now - self._drift_at) // every)
        if steps <= 0:
            return False
        self._drift_at += steps * every  # all of it: drift stops at the centre anyway
        before = self.meter
        if self.meter > CENTRE:
            self.meter = max(CENTRE, self.meter - steps)
        elif self.meter < CENTRE:
            self.meter = min(CENTRE, self.meter + steps)
        return self.meter != before

    def _switch_if_due(self, now: float) -> bool:
        if now < self.locked_until:
            return False
        laws = self.laws()
        if self.mode == "anarchy" and self.meter >= laws.meter_democracy_at:
            self._become("democracy", now)
            return True
        if self.mode == "democracy" and self.meter <= laws.meter_anarchy_at:
            self._become("anarchy", now)
            return True
        return False

    def _become(self, mode: Mode, now: float) -> None:
        self.mode = mode
        self.locked_until = now + self.laws().mode_lock_s
        self.window_ends, self.ballots = None, {}

    # --- moderators and brain ----------------------------------------------------------

    def set_mode(self, mode: Mode, now: float) -> Reply:
        """A moderator's /mode: switch now, the meter to the threshold, locked as after any switch."""
        if mode == self.mode:
            return Reply(False, f"It's already {mode}.")
        laws = self.laws()
        if mode == "democracy":
            self.meter = max(self.meter, float(laws.meter_democracy_at))
        else:
            self.meter = min(self.meter, float(laws.meter_anarchy_at))
        self._become(mode, now)
        self.on_change()
        return Reply(True, f"Mode: {mode} (locked for {laws.mode_lock_s} s).")

    def clear(self) -> None:
        """Drop this window's ballots (a panic, a rewind: they were for another screen)."""
        if self.ballots or self.window_ends is not None:
            self.window_ends, self.ballots = None, {}
            self.on_change()

    def tally(self) -> list[tuple[str, int, Ballot]]:
        """Each command, its votes and its earliest ballot: most votes first, then the earliest."""
        counts: dict[str, int] = {}
        first: dict[str, Ballot] = {}
        for ballot in self.ballots.values():
            counts[ballot.text] = counts.get(ballot.text, 0) + 1
            if ballot.text not in first or ballot.at < first[ballot.text].at:
                first[ballot.text] = ballot
        return sorted(((t, n, first[t]) for t, n in counts.items()), key=lambda x: (-x[1], x[2].at))

    def winner(self) -> Winner | None:
        ranked = self.tally()
        if not ranked:
            return None
        text, votes, ballot = ranked[0]
        return Winner(text, votes, len(self.ballots), ballot)

    def view(self, now: float) -> dict[str, Any]:
        laws = self.laws()
        return {
            "mode": self.mode,
            "meter": round(self.meter),
            "democracy_at": laws.meter_democracy_at,
            "anarchy_at": laws.meter_anarchy_at,
            "locked_for": round(max(0.0, self.locked_until - now)),
            "tally": {
                "ends_in": round(max(0.0, self.window_ends - now), 1) if self.window_ends else None,
                "options": [{"text": t, "votes": n} for t, n, _ in self.tally()[:TALLY_SHOWN]],
                "total": len(self.ballots),
            }
            if self.mode == "democracy"
            else None,
        }
