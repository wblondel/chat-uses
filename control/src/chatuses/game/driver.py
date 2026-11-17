"""The driver's seat (PLAN.md 6.9, Phase 9): a minute at the wheel for one of chat.

Every driver_interval_min minutes (a law), brain offers the seat to someone
picked at random among those who are active now (banned people never are),
have had MIN_COMMANDS commands accepted this session, and haven't held the
seat in the last REST seconds. They have ACCEPT seconds to take it by sending
any command; that command runs, and for driver_duration_s seconds (a law)
only their input reaches the machine: everyone else's is refused with how
long is left. Votes and the meter work as ever: `!impeach`, a vote, ends a
drive early.

No offer while brain says it can't be (the machine quiet: a panic, travel, a
death, a run's end; democracy; the feature off): the next one is due an
interval later. Brain ends a drive when democracy begins.

Everything is driven by `now`: brain ticks it with its referee; tests and the
simulator pass a fake clock.
"""

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from chatuses.identity import Presence
from chatuses.referee.laws import Laws

ACCEPT = 15.0  # seconds to take an offered seat
REST = 3600.0  # seconds before someone may be offered it again
MIN_COMMANDS = 3  # commands accepted this session, at least

Event = Literal["offered", "took", "declined", "ended", "impeached", "stopped"]


@dataclass
class Seat:
    user: str
    name: str  # as the overlay shows them
    state: Literal["offered", "driving"]
    until: float  # the offer's end, or the drive's
    since: float


class DriverSeat:
    def __init__(
        self,
        laws: Callable[[], Laws],
        presence: Presence,
        commands: Callable[[float], dict[str, int]],
        on_change: Callable[[Event, Seat], None] = lambda _event, _seat: None,
        rng: random.Random | None = None,
    ) -> None:
        self.laws = laws
        self.presence = presence
        self.commands = commands  # now -> accepted commands this session, by user
        self.on_change = on_change  # brain: the overlay, Discord, the log
        self.rng = rng or random.Random()
        self.seat: Seat | None = None
        self.next_at: float | None = None  # when the next offer is due
        self._held: dict[str, float] = {}  # user -> when their last offer or drive ended

    def candidates(self, now: float) -> list[str]:
        counts = self.commands(now)
        return sorted(
            user
            for user in self.presence.active(now)
            if counts.get(user, 0) >= MIN_COMMANDS and now - self._held.get(user, -math.inf) >= REST
        )

    def tick(self, now: float, can_offer: bool, name_of: Callable[[str], str]) -> None:
        """Time passes: an offer or a drive runs out, or an offer is due."""
        seat = self.seat
        if seat is not None:
            if now >= seat.until:
                self._finish(now, "declined" if seat.state == "offered" else "ended")
            return
        interval = self.laws().driver_interval_min * 60
        if self.next_at is None:
            self.next_at = now + interval
        if now < self.next_at:
            return
        self.next_at = now + interval  # an offer or not, the next is an interval away
        candidates = self.candidates(now) if can_offer else []
        if not candidates:
            return
        user = self.rng.choice(candidates)
        self.seat = Seat(user, name_of(user), "offered", now + ACCEPT, now)
        self.on_change("offered", self.seat)

    def command(self, user: str, now: float) -> str | None:
        """An input command from chat: None if it may run, else why not (for chat).
        The one the seat is offered to takes it with this command."""
        seat = self.seat
        if seat is None:
            return None
        if seat.state == "offered":
            if user == seat.user and now < seat.until:
                seat.state, seat.since = "driving", now
                seat.until = now + self.laws().driver_duration_s
                self.on_change("took", seat)
            return None  # an offer doesn't stop anyone yet
        if user == seat.user:
            return None
        return f"{seat.name} is driving: {math.ceil(max(0.0, seat.until - now))} s left"

    def end(self, now: float, why: Literal["impeached", "stopped"]) -> bool:
        """A vote (impeached) or brain (stopped: democracy, a panic) ends it. False if no one had it."""
        if self.seat is None:
            return False
        self._finish(now, why)
        return True

    def _finish(self, now: float, event: Event) -> None:
        seat = self.seat
        if seat is None:
            return
        self._held[seat.user] = now
        self.seat = None
        self.next_at = now + self.laws().driver_interval_min * 60
        self.on_change(event, seat)

    def view(self, now: float) -> dict[str, Any] | None:
        """For the overlay: who, offered or driving, how long left."""
        seat = self.seat
        if seat is None:
            return None
        return {"name": seat.name, "state": seat.state, "ends_in": round(max(0.0, seat.until - now), 1)}
