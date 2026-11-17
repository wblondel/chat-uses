"""The driver's seat (Phase 9 step 3, PLAN.md 6.9): an offer, a drive, an impeachment."""

import asyncio
import random
import time
from collections.abc import Callable, Coroutine
from typing import Any, cast

from chatuses.adapters.discord_bot import DiscordChat
from chatuses.brain import Brain
from chatuses.commands import TypeText
from chatuses.executor import connect_to
from chatuses.game.driver import ACCEPT, MIN_COMMANDS, REST, DriverSeat, Event, Seat
from chatuses.identity import Presence
from chatuses.referee.laws import Laws
from chatuses.store import Store

T = 1_800_000_000.0
LAWS = Laws(driver_interval_min=10, driver_duration_s=60)


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


class Seats:
    """A DriverSeat with everyone in `counts` active, and its events."""

    def __init__(self, counts: dict[str, int]) -> None:
        self.presence = Presence()
        for user in counts:
            self.presence.saw(user, T - 600)
            self.presence.saw(user, T - 5)
        self.counts = counts
        self.events: list[tuple[Event, str]] = []
        self.seat = DriverSeat(
            lambda: LAWS,
            self.presence,
            lambda _now: self.counts,
            lambda event, seat: self.events.append((event, seat.user)),
            random.Random(1),
        )

    def tick(self, now: float, can_offer: bool = True) -> None:
        for user in self.counts:  # they keep talking: active
            self.presence.saw(user, now - 5)
        self.seat.tick(now, can_offer, lambda user: user.upper())


def test_an_offer_every_interval_to_someone_who_played() -> None:
    seats = Seats({"yt:a": 5, "yt:b": MIN_COMMANDS - 1})
    seats.tick(T)  # the clock starts: the first offer is an interval away
    seats.tick(T + 599)
    assert seats.seat.seat is None
    seats.tick(T + 600)
    seat = seats.seat.seat
    assert seat is not None and (seat.user, seat.name, seat.state) == ("yt:a", "YT:A", "offered")
    assert seats.seat.candidates(T + 600) == ["yt:a"]  # yt:b sent 2 commands: never
    assert seats.seat.view(T + 605) == {"name": "YT:A", "state": "offered", "ends_in": ACCEPT - 5}


def test_any_command_takes_it_then_only_the_driver_s_run() -> None:
    seats = Seats({"yt:a": 5, "yt:b": 5})
    seats.seat.next_at = T
    seats.tick(T)
    seat = seats.seat.seat
    assert seat is not None
    driver, other = seat.user, ({"yt:a", "yt:b"} - {seat.user}).pop()
    assert seats.seat.command(other, T + 1) is None  # offered: others still play
    assert seats.seat.command(driver, T + 2) is None  # taken
    assert seat.state == "driving" and seat.until == T + 62
    assert seats.seat.command(other, T + 3) == f"{seat.name} is driving: 59 s left"
    assert seats.seat.command(driver, T + 3) is None
    seats.tick(T + 62)
    assert seats.seat.seat is None and seats.events == [
        ("offered", driver),
        ("took", driver),
        ("ended", driver),
    ]
    assert seats.seat.command(other, T + 63) is None


def test_an_offer_runs_out_and_nobody_holds_it_twice_in_an_hour() -> None:
    seats = Seats({"yt:a": 5})
    seats.seat.next_at = T
    seats.tick(T)
    seats.tick(T + ACCEPT)
    assert seats.events == [("offered", "yt:a"), ("declined", "yt:a")]
    assert seats.seat.command("yt:a", T + ACCEPT + 1) is None  # too late: not taken
    seats.tick(T + 600 + ACCEPT)
    assert seats.seat.seat is None and seats.seat.candidates(T + 600 + ACCEPT) == []  # resting
    seats.tick(T + ACCEPT + REST + 600)
    assert seats.seat.seat is not None


def test_no_offer_when_brain_says_not_and_impeachment() -> None:
    seats = Seats({"yt:a": 5})
    seats.seat.next_at = T
    seats.tick(T, can_offer=False)
    assert seats.seat.seat is None and seats.seat.next_at == T + 600  # the next, an interval later
    seats.tick(T + 600)
    assert seats.seat.command("yt:a", T + 601) is None
    assert seats.seat.end(T + 610, "impeached") and seats.seat.seat is None
    assert seats.events[-1] == ("impeached", "yt:a")
    assert not seats.seat.end(T + 611, "stopped")


# --- through brain -------------------------------------------------------------------------


def brain_on(store: Store) -> Brain:
    brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
    brain.discord = DiscordChat("token", 1, "control", lambda _m: None, lambda: "")
    brain.executor.online.set()  # as if the machine answered
    sent: list[dict[str, Any]] = []
    brain.overlay._send = sent.append  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    brain.sent = sent  # type: ignore[attr-defined]
    now = time.time()
    for user in ("dc:42", "console:bob", "console:carol", "console:dan"):
        brain.pipeline.presence.saw(user, now - 600)
        brain.pipeline.presence.saw(user, now - 5)
        store.seen_user(user, user.split(":")[1].capitalize(), now - 600, frozenset())
    return brain


def offer_to(brain: Brain, user: str) -> Seat:
    for i in range(MIN_COMMANDS):
        brain.store.record_command(time.time() - 10 + i, user, "!type x", TypeText("x"), "accepted", None)
    brain.driver.next_at = time.time() - 1
    brain.driver.candidates = lambda _now: [user]  # type: ignore[method-assign]
    brain.driver_tick(time.time())
    seat = brain.driver.seat
    assert seat is not None and seat.user == user
    return seat


def test_a_discord_driver_is_pinged_and_drives_alone_until_impeached() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            seat = offer_to(brain, "dc:42")
            assert brain.discord is not None and brain.discord.pinged[0][0] == 42
            assert "the driver's seat is yours" in brain.discord.pinged[0][1]
            sent = cast(list[dict[str, Any]], brain.__dict__["sent"])
            assert [m for m in sent if m["type"] == "driver"][-1]["driver"]["state"] == "offered"
            assert brain.overlay._driver_now() is not None  # pyright: ignore[reportPrivateUsage]
            brain.handle(_msg("dc:42", "!enter"), "dc")
            assert seat.state == "driving"
            assert "Driver's seat: 42 drives" in brain.status_text()
            outcome = await brain.console_message("bob", "!click F7")
            assert outcome == ("refused", f"{seat.name} is driving: 60 s left")
            assert (await brain.console_message("bob", "!impeach")) == ("accepted", None)
            for voter in ("carol", "dan"):
                await brain.console_message(voter, "!yes")
            vote = brain.referee.votes.current
            assert vote is not None
            opened = vote.opened
            brain.referee.clock = lambda: opened + vote.tier.window_s + 1
            brain.referee.tick()
            for _ in range(20):
                await asyncio.sleep(0.01)
                if vote.phase == "done":
                    break
            assert vote.result == f"{seat.name} is out of the driver's seat" and brain.driver.seat is None
            events = [payload for _id, _ts, kind, payload in store.events(limit=50) if kind == "driver"]
            assert [e["phase"] for e in events] == ["offered", "took", "impeached"]
            store.forget(time.time(), "dc:42")
            assert not [
                kind for _id, _ts, kind, _p in store.events(limit=50) if kind == "driver"
            ]  # theirs go

    run(test)


def test_democracy_or_a_panic_stops_a_drive_and_nobody_impeaches_an_empty_seat() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            assert await brain.console_message("bob", "!impeach") == ("refused", "nobody is driving")
            offer_to(brain, "console:carol")
            brain.referee.set_mode("democracy", "a moderator")
            brain.driver_tick(time.time())
            assert brain.driver.seat is None and "Driver's seat: empty, next offer in" in brain.status_text()
            brain.referee.set_mode("anarchy", "a moderator")
            offer_to(brain, "console:dan")
            brain.moderation.panic("console", "test")
            brain.driver_tick(time.time())
            assert brain.driver.seat is None

    run(test)


def _msg(user: str, text: str) -> Any:
    from chatuses.pipeline import ChatMessage

    return ChatMessage(user, user, text, time.time())
