"""Achievements and points (Phase 8 step 2)."""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fake_incus import FakeTarget

from chatuses.adapters.discord_bot import DiscordChat
from chatuses.brain import Brain
from chatuses.commands import TypeText
from chatuses.executor import Job, connect_to
from chatuses.game import achievements as module
from chatuses.game.achievements import Achievement, AchievementError, Context, load, parse, passes
from chatuses.game.points import Points
from chatuses.identity import Presence
from chatuses.store import Store

REPO = Path(__file__).resolve().parents[2]
T = 1_800_000_000.0


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


# --- definitions -------------------------------------------------------------------------


def test_the_starter_set_loads() -> None:
    found = load(REPO / "achievements")
    assert len(found) == 15 and len({a.key for a in found}) == 15
    doom = next(a for a in found if a.key == "doom_guy")
    assert (doom.points, doom.lives, doom.command()) == (50, 1, ["pgrep", "--", doom.check["pattern"]])
    assert next(a for a in found if a.key == "survivor_6h").check == {
        "type": "uptime_since_rewind",
        "hours": 6.0,
    }


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("{}", "a list of achievements"),
        ("- {key: Bad, name: x, description: y, check: {type: http}}", "key must be"),
        ("- {key: a, name: x, description: y, check: {type: telepathy}}", "check type must be one of"),
        ("- {key: a, name: x, description: y, check: {type: process}}", "takes ['pattern']"),
        ("- {key: a, name: x, description: y, check: {type: process, pattern: '('}}", "pattern:"),
        ("- {key: a, name: x, description: y, check: {type: saves, count: 0}}", "above 0"),
        ("- {key: a, name: x, description: y, check: {type: http}, reward: {points: 5000}}",
         "points: 0 to 1000"),
        ("- {key: a, name: x, description: y, check: {type: http}, reward: {lives: 3}}", "lives: 0 to 2"),
        ("- {key: a, name: x, description: y, check: {type: http}, extra: 1}", "unknown keys"),
        ("- {key: a, name: x, description: y, check: {type: http}}\n"
         "- {key: a, name: z, description: w, check: {type: http}}", "the same key twice"),
    ],
)  # fmt: skip
def test_bad_definitions_are_refused_with_the_reason(text: str, error: str) -> None:
    with pytest.raises(AchievementError, match=None) as caught:
        parse(text)
    assert error in str(caught.value)


def test_checks_in_the_target_count_only_their_exit_status() -> None:
    async def test() -> None:
        ran: list[list[str]] = []

        async def exec_(command: list[str]) -> int:
            ran.append(command)
            return 0 if command[0] == "test" else 1

        ctx = Context(exec_, lambda: "Hello World!", lambda: 7200.0, lambda: 2)
        [file_, proc, ocr, survivor, saves] = parse(
            """
- {key: f, name: F, description: d, check: {type: file_exists, path: /tmp/x}}
- {key: p, name: P, description: d, check: {type: process, pattern: '^cmatrix$'}}
- {key: o, name: O, description: d, check: {type: ocr_text, pattern: 'hello,? world'}}
- {key: s, name: S, description: d, check: {type: uptime_since_rewind, hours: 1}}
- {key: v, name: V, description: d, check: {type: saves, count: 3}}
"""
        )
        assert await passes(file_, ctx) and not await passes(proc, ctx)
        assert await passes(ocr, ctx) and await passes(survivor, ctx) and not await passes(saves, ctx)
        assert ran == [["test", "-e", "/tmp/x"], ["pgrep", "--", "^cmatrix$"]]

        async def hangs(_command: list[str]) -> int:
            await asyncio.sleep(10)
            return 0

        module.CHECK_TIMEOUT, saved = 0.05, module.CHECK_TIMEOUT
        try:
            assert not await passes(file_, Context(hangs, str, float, int))  # a check that hangs: a no
        finally:
            module.CHECK_TIMEOUT = saved

    run(test)


# --- points ------------------------------------------------------------------------------


def test_points_a_minute_and_per_command_with_a_cap() -> None:
    with Store(":memory:") as store:
        presence = Presence(is_banned=lambda user: user == "yt:banned")
        for user, at in (("yt:a", T - 30), ("dc:b", T - 50), ("yt:quiet", T - 200), ("yt:banned", T - 10)):
            store.seen_user(user, user, at, frozenset())
            presence.saw(user, at)
        points = Points(store, presence)
        assert points.active_minute(T) == 2
        earned = [points.command_ran("yt:a", T + i) for i in range(7)]
        assert earned == [2, 2, 2, 2, 2, 0, 0]  # 10 a minute at most
        assert points.command_ran("yt:a", T + 60) == 2  # a new minute
        assert store.standing("yt:a") == (13, 1, 2) and store.standing("dc:b") == (1, 2, 2)
        assert store.standing("yt:quiet") == (0, 0, 2)
        assert [r["user"] for r in store.leaderboard(now=T)] == ["yt:a", "dc:b"]
        store.time_out(T, "yt:a", T + 600, "spam")
        assert [r["user"] for r in store.leaderboard(now=T)] == ["dc:b"]  # not while sanctioned
        assert [r["user"] for r in store.leaderboard(now=T + 601)] == ["yt:a", "dc:b"]


# --- through brain -------------------------------------------------------------------------


@asynccontextmanager
async def brain_with(achievements: list[Achievement]) -> AsyncGenerator[Brain]:
    with Store(":memory:") as store:
        target = FakeTarget()
        target.exit_codes = {}  # type: ignore[attr-defined]
        brain = Brain(
            store,
            lambda: connect_to("127.0.0.1", 1),
            target,
            target_instance="desk",
            achievements=achievements,
        )
        brain.discord = DiscordChat("token", 1, "control", lambda _m: None, lambda: "")
        sent: list[dict[str, Any]] = []
        brain.overlay._send = sent.append  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        brain.sent = sent  # type: ignore[attr-defined]
        yield brain


def test_an_unlock_credits_who_played_gives_points_and_a_life_and_says_so() -> None:
    async def test() -> None:
        defs = parse(
            """
- {key: doom_guy, name: Doom Guy, description: Get Freedoom running,
   check: {type: process, pattern: doom}, reward: {points: 50, lives: 1}}
- {key: hello, name: Hello, description: Say hello, check: {type: ocr_text, pattern: hello}}
"""
        )
        async with brain_with(defs) as brain:
            now = time.time()
            for user, ts in (("yt:a", now - 60), ("dc:b", now - 30), ("yt:old", now - 3600)):
                brain.store.seen_user(user, user, ts, frozenset())
                brain.store.record_command(ts, user, "!type x", TypeText("x"), "accepted", None)
            ran: list[list[str]] = []

            async def exec_(_instance: str, command: list[str]) -> int:
                ran.append(command)
                return 0

            brain.incus.exec = exec_  # type: ignore[method-assign, union-attr]
            unlocked = await brain.check_achievements()
            assert [a.key for a in unlocked] == ["doom_guy"] and ran == [["pgrep", "--", "doom"]]
            assert await brain.check_achievements() == []  # once a run
            run_ = brain.runs.current
            assert run_ is not None
            assert brain.store.unlocked(run_["id"])["doom_guy"]["credited"] == ["dc:b", "yt:a"]
            assert brain.store.standing("yt:a")[0] == 50 and brain.store.standing("yt:old")[0] == 0
            assert brain.runs.lives_left() == 4
            [toast] = [m for m in brain.sent if m["type"] == "achievement"]  # type: ignore[attr-defined]
            assert toast["name"] == "Doom Guy" and toast["rewards"] == ["+50 points each for 2", "+1 life"]
            assert brain.discord is not None
            [post] = [t for t, _i, ch, _h in brain.discord._alerts if ch == "achievements"]  # pyright: ignore[reportPrivateUsage]
            assert post.startswith("🏆 **Doom Guy** (run #1): Get Freedoom running.")
            assert "✓ Doom Guy" in brain.achievements_text() and "· Hello" in brain.achievements_text()
            assert brain.leaderboard_text().split("\n") == ["1. dc:b: 50", "2. yt:a: 50"]
            assert brain.me_text("yt:a").startswith("You have 50 points: #1 of 2.")
            assert "you helped unlock: Doom Guy" in brain.me_text("yt:a")
            assert brain.me_text("yt:nobody").startswith("No points yet")

    run(test)


def test_no_checks_while_the_machine_is_dying_or_the_feature_is_off() -> None:
    async def test() -> None:
        defs = parse("- {key: h, name: H, description: d, check: {type: ocr_text, pattern: x}}")
        async with brain_with(defs) as brain:
            brain.watch = None
            brain.runs.state = "dying"
            assert await brain.check_achievements() == []
            brain.runs.state = "alive"
            from dataclasses import replace

            brain.referee.ruleset = replace(brain.referee.ruleset, features=frozenset({"anarchy"}))
            assert await brain.check_achievements() == []

    run(test)


def test_a_command_that_ran_earns_points() -> None:
    async def test() -> None:
        async with brain_with([]) as brain:
            brain.store.seen_user("yt:a", "a", time.time(), frozenset())
            brain._executed(Job("yt:a", TypeText("x")), "done", 0.1)  # pyright: ignore[reportPrivateUsage]
            brain._executed(Job("yt:a", TypeText("x")), "flushed", 0.1)  # pyright: ignore[reportPrivateUsage]
            assert brain.store.standing("yt:a")[0] == 2

    run(test)


def test_a_deletion_request_takes_the_points_and_the_credits() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "a", T, frozenset())
        store.seen_user("dc:b", "b", T, frozenset())
        run_ = store.open_run(T, "roguelike", "desk", 3)
        assert store.unlock(run_["id"], "doom_guy", T, ["dc:b", "yt:a"], 50)
        assert not store.unlock(run_["id"], "doom_guy", T + 1, ["yt:a"], 50)
        store.forget(T + 10, "yt:a")
        assert store.unlocked(run_["id"])["doom_guy"]["credited"] == ["dc:b"]
        assert store.standing("yt:a")[0] == 0 and store.standing("dc:b")[0] == 50
