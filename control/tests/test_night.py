"""Night mode (PLAN.md 7.6, safety/night.py): when it falls and lifts, what it changes."""

import asyncio
from collections.abc import Iterator

import pytest

from chatuses.commands import Meta
from chatuses.identity import Presence
from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.ratelimit import RateLimiter
from chatuses.referee.laws import DEFAULT_TIERS, Laws
from chatuses.referee.referee import NIGHT_MODE, Referee
from chatuses.referee.votes import Proposal
from chatuses.safety.night import AWAY, HERE, Change, Night, stricter
from chatuses.store import Store

T = 1_800_000_000.0


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as s:
        yield s


def test_night_falls_without_moderators_and_lifts_with_one(store: Store) -> None:
    night = Night(store, T)  # a new brain: as if a moderator had just left
    assert night.tick(T) is None and not night.on
    assert night.tick(T + AWAY - 1) is None
    fell = night.tick(T + AWAY)
    assert fell is not None and fell.on and fell.why.startswith("no moderator around since ")
    assert night.on and night.tick(T + AWAY + 60) is None  # told once
    assert "NIGHT since" in night.status_line(T + AWAY + 60)
    night.saw("Ana", T + AWAY + 120)
    assert night.tick(T + AWAY + 120) == Change(False, "Ana is here")
    line = night.status_line(T + AWAY + 180)
    assert "day (a moderator seen: Ana, " in line and "night in 14 min without one" in line


def test_here_counts_for_an_hour_and_a_later_word_doesnt_shorten_it(store: Store) -> None:
    night = Night(store, T)
    night.saw("Ana", T, HERE)
    night.saw("Bo", T + 60)  # a message: 15 minutes, but the hour stands
    assert night.tick(T + HERE - 1) is None
    fell = night.tick(T + HERE)
    assert fell is not None and fell.on


def test_on_and_off_are_kept_and_win_over_who_is_around(store: Store) -> None:
    night = Night(store, T)
    assert night.set("dusk", "Ana") == (False, "Night mode is auto, on or off.")
    assert night.set("auto", "Ana") == (False, "Night mode is already auto.")
    assert night.set("on", "Ana")[0]
    night.saw("Bo", T)
    assert night.tick(T) == Change(True, "set on by Ana")
    assert night.status_line(T).startswith("Night mode: on (by Ana): NIGHT since")
    again = Night(store, T + 10)  # a restart
    assert again.setting == "on" and again.tick(T + 10) == Change(True, "set on by Ana")
    assert again.set("off", "Cy")[0] and again.tick(T + AWAY * 10) == Change(False, "set off by Cy")
    assert again.set("auto", "Cy")[0] and store.setting("night_mode") is None
    fell = again.tick(T + AWAY * 10)  # nobody around since T
    assert fell is not None and fell.on and fell.why.startswith("no moderator around since")


def test_a_restart_keeps_when_a_moderator_was_last_around(store: Store) -> None:
    first = Night(store, T)
    first.saw("Ana", T + 100)  # written: the first sighting a minute after the start
    second = Night(store, T + 200)
    assert second.by == "Ana" and second.until == T + 100 + AWAY
    assert second.tick(T + 100 + AWAY) is not None
    fresh = Night(store, T + 10_000)  # restarts don't push the night back
    assert fresh.tick(T + 10_000) is not None and fresh.on
    store.set_setting("moderator_seen", "not json")
    assert Night(store, T).until == T + AWAY  # unreadable: as if one had just left


def test_stricter_laws_need_more_people() -> None:
    laws = stricter(Laws())
    rewind, default = laws.tier("rewind"), DEFAULT_TIERS["rewind"]
    assert (rewind.quorum_floor, rewind.quorum_fraction) == (6, 0.375)
    assert (rewind.threshold, rewind.window_s) == (default.threshold, default.window_s)
    assert rewind.quorum(12) == 6 and default.quorum(12) == 3
    assert laws.tier("newrun").quorum_fraction == 0.525
    assert laws.democracy_window_s == Laws().democracy_window_s  # only the votes change


def test_night_rate_limits(store: Store) -> None:
    limiter = RateLimiter()
    pipeline = Pipeline(store, limiter=limiter)
    assert [limiter.allow("a", 0) for _ in range(3)] == ["ok", "ok", "ok"]
    limiter.set_night(True)
    assert limiter.every == 4
    assert limiter.allow("a", 0.1) == "user"  # what was spent stays spent
    assert [limiter.allow("b", 0.1) for _ in range(3)] == ["ok", "ok", "user"]  # a burst of 2
    refused = pipeline.handle(ChatMessage("b", "b", "!click F7", 0.2))
    assert refused.reason == "slow down: one command every 4 seconds"
    assert limiter.allow("a", 4.2) == "ok"  # one every 4 s
    limiter.set_night(False)
    assert limiter.every == 2
    said = [pipeline.handle(ChatMessage("d", "d", "!click F7", 9)) for _ in range(4)]
    assert [o.kind for o in said] == ["accepted"] * 3 + ["rate_limited"]  # a burst of 3 again
    assert said[-1].reason == "slow down: one command every 2 seconds"


class Actions:
    def unavailable(self, proposal: Proposal) -> str | None:
        return None

    async def run(self, proposal: Proposal) -> tuple[bool, str]:
        return True, "done"


def test_the_referee_at_night(store: Store) -> None:
    async def test() -> None:
        now = [T]
        presence = Presence()
        referee = Referee(store, presence, Actions(), clock=lambda: now[0])
        for user in ("a", "b"):
            presence.saw(user, T - 600)
            presence.saw(user, T)
        assert not referee.democracy
        referee.set_night(True)
        assert referee.democracy and referee.modes.locked_until > T
        assert referee.laws.tier("reboot").quorum_floor == 4
        assert referee.base_laws.tier("reboot").quorum_floor == 2
        assert referee.handle("a", "a", Meta("anarchy"), T).text == NIGHT_MODE
        assert referee.handle("a", "a", Meta("democracy"), T).text == NIGHT_MODE
        assert "Ruleset: roguelike" in referee.status_line()
        referee.set_night(False)
        assert not referee.democracy and referee.laws.tier("reboot").quorum_floor == 2
        # Democracy before the night: still democracy after it.
        referee.set_mode("democracy", "a moderator")
        referee.set_night(True)
        referee.set_night(False)
        assert referee.democracy

    asyncio.run(test())
