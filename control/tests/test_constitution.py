"""The constitution (Phase 9 step 2, PLAN.md 6.10): !amend, its bounds, its votes, its record."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from chatuses.brain import Brain
from chatuses.commands import Amend, ParseError
from chatuses.executor import connect_to
from chatuses.grid import Grid
from chatuses.parser import parse
from chatuses.referee import constitution
from chatuses.referee.constitution import UNKNOWN, AmendError, check, clauses
from chatuses.referee.laws import DEFAULT, Laws, apply
from chatuses.site import Site, gather
from chatuses.store import Store

REPO = Path(__file__).resolve().parents[2]


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


# --- what chat may change -----------------------------------------------------------------


def test_the_clauses_and_their_bounds() -> None:
    found = clauses(DEFAULT)
    assert (found["democracy_window"].key, found["democracy_window"].bounds()) == (
        "democracy_window_s",
        "8 to 30 s",
    )
    assert found["lives"].bounds() == "1 to 5"
    assert (found["reboot_threshold"].low, found["reboot_threshold"].high) == (50, 60)  # never under half
    assert (found["rewind_threshold"].low, found["rewind_threshold"].high) == (56, 76)  # 66 ± 10
    assert "newrun_threshold" not in found and "amend_threshold" not in found  # the Bill of Rights
    assert not any("quorum" in name or "veto" in name for name in found)


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("quorum", 3, UNKNOWN),
        ("newrun_threshold", 70, UNKNOWN),
        ("democracy_window", 60, "democracy_window can be 8 to 30 s"),
        ("democracy_window", 7, "democracy_window can be 8 to 30 s"),
        ("democracy_window", 12.5, "democracy_window takes a whole number"),
        ("democracy_window", 12, "democracy_window is 12 already"),
        ("rewind_threshold", 90, "rewind_threshold can be 56 to 76 %"),
        ("anarchy_at", 40, "anarchy_at can be 10 to 30"),
    ],
)
def test_what_chat_can_t_amend_is_refused_in_fixed_words(name: str, value: float, why: str) -> None:
    with pytest.raises(AmendError) as caught:
        check(DEFAULT, DEFAULT.laws, name, value)
    assert str(caught.value) == why


def test_a_threshold_is_written_as_a_percentage_and_kept_as_a_fraction() -> None:
    clause, law = check(DEFAULT, DEFAULT.laws, "rewind_threshold", 70)
    assert (clause.key, law) == ("tiers.rewind.threshold", 0.7)
    amended = apply(DEFAULT.laws, {clause.key: law})
    assert constitution.current(clause, amended) == 70
    assert check(DEFAULT, Laws(), "reboot_threshold", 55.5)[1] == pytest.approx(0.555)


def test_the_grammar_and_refusals_that_never_repeat_chat() -> None:
    grid = Grid()
    assert parse("!amend Democracy_Window 15", grid) == Amend("democracy_window", 15.0)
    assert parse("!amend rewind_threshold 70.5", grid) == Amend("rewind_threshold", 70.5)
    for text in (
        "!amend",
        "!amend xyzzy",
        "!amend xyzzy 1 2",
        "!amend xyzzy -3",
        "!amend 1xyzzy 3",
        "!amend x 3",
    ):
        parsed = parse(text, grid)
        assert isinstance(parsed, ParseError) and "xyzzy" not in parsed.reason, text


# --- through brain ----------------------------------------------------------------------


def brain_on(store: Store, people: int = 5) -> Brain:
    brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
    now = time.time()
    for i in range(people):
        brain.pipeline.presence.saw(f"console:p{i}", now - 600)
        brain.pipeline.presence.saw(f"console:p{i}", now - 5)
    return brain


async def amend(brain: Brain, text: str, yes: int = 4, vetoes: int = 0) -> str:
    """p0 proposes, p1..p{yes-1} say yes; the window and the veto countdown pass; how it ended."""
    answer = await brain.console_message("p0", text)
    if answer != ("accepted", None):
        return f"refused: {answer[1]}"
    for i in range(1, yes):
        await brain.console_message(f"p{i}", "!yes")
    vote = brain.referee.votes.current
    assert vote is not None
    opened, tier = vote.opened, vote.tier
    brain.referee.clock = lambda: opened + tier.window_s + 1
    brain.referee.tick()
    for i in range(vetoes):
        await brain.console_message(f"p{yes + i}", "!veto")
    brain.referee.clock = lambda: opened + tier.window_s + tier.veto_s + 2
    brain.referee.tick()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if vote.phase in ("done", "error", "failed", "vetoed"):
            break
    brain.referee.clock = time.time
    return f"{vote.phase}: {vote.result}"


def test_chat_amends_a_law_numbered_kept_and_shown() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            assert await amend(brain, "!amend nosuchlaw 3") == f"refused: {UNKNOWN}"
            result = await amend(brain, "!amend democracy_window 20")
            assert result == "done: Amendment #1: democracy_window is 20 s"
            assert brain.referee.laws.democracy_window_s == 20
            assert store.laws()["democracy_window_s"] == 20
            [kept] = store.amendments()
            assert (kept["number"], kept["name"], kept["value"], kept["by"]) == (
                1,
                "democracy_window",
                20,
                "console:p0",
            )
            again = brain_on(store)  # a restart keeps it
            assert again.referee.laws.democracy_window_s == 20
            assert "democracy_window_s = 20" in (await brain.admin(["laws"]))[1]

    run(test)


def test_an_amendment_can_fail_or_be_vetoed_and_waits_an_hour() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store, people=9)
            assert (await amend(brain, "!amend lives 5", yes=3)).startswith("failed")  # 4 at least
            brain.referee.votes._cooldown.clear()  # pyright: ignore[reportPrivateUsage]
            assert (await amend(brain, "!amend lives 5", yes=4, vetoes=3)).startswith("vetoed")
            assert store.amendments() == [] and brain.referee.laws.lives_start == 3
            assert (await amend(brain, "!amend lives 4")).startswith("refused: !amend was just voted on")

    run(test)


def test_lives_change_from_the_next_run_and_thresholds_apply_to_the_next_vote() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            assert await amend(brain, "!amend lives 5") == "done: Amendment #1: lives is 5 from the next run"
            assert brain.runs.lives_left() == 3  # this run keeps its lives
            brain.referee.votes._cooldown.clear()  # pyright: ignore[reportPrivateUsage]
            assert (await amend(brain, "!amend reset_threshold 70")).endswith("reset_threshold is 70 %")
            assert brain.referee.laws.tier("reset").threshold == pytest.approx(0.7)
            run_ = brain.runs.start("roguelike", "desk")
            assert run_["lives_start"] == 5
            assert [a["number"] for a in store.amendments()] == [1, 2]

    run(test)


def test_a_deletion_request_keeps_the_amendment_without_its_proposer() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "a", time.time(), frozenset())
        store.add_amendment(time.time(), "lives", "lives_start", 4, "yt:a", None)
        store.forget(time.time(), "yt:a")
        assert [(a["number"], a["by"]) for a in store.amendments()] == [(1, "")]


def test_the_site_shows_what_chat_may_amend_and_the_amendments() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "Alice", time.time(), frozenset())
        store.add_amendment(1_800_000_000, "rewind_threshold", "tiers.rewind.threshold", 0.7, "yt:a", None)
        store.set_law(1_800_000_000, "tiers.rewind.threshold", 0.7, "amendment #1")
        brain = brain_on(store)
        data = gather(
            store,
            achievements=[],
            ruleset=brain.referee.ruleset,
            laws=brain.referee.laws,
            safe_name=brain.filter.safe_name,
            lives_left=3,
        )
        page = Site(Path("/nonexistent"), REPO / "site", None).render(data)[0]["laws.html"].decode()
        assert "<code>!amend democracy_window</code> 8 to 30 s" in page
        assert "<code>!amend rewind_threshold</code> 56 to 76 %" in page
        assert "<strong>Amendment #1</strong>: <code>rewind_threshold 70</code>" in page
        assert "proposed by Alice" in page
        assert '<code>!rewind</code></td><td class="num">70 %</td>' in page  # the law, amended
