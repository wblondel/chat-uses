"""A run's life through brain (Phase 8 step 1): deaths, revives, the end, the next run."""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fake_incus import FakeTarget

from chatuses import brain as brain_module
from chatuses.adapters.discord_bot import DiscordChat
from chatuses.brain import DYING, NO_LIVES, Brain
from chatuses.executor import connect_to
from chatuses.game import runs as runs_module
from chatuses.game.runs import Death
from chatuses.store import Store


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs_module, "DEATH_AFTER", 0.0)
    monkeypatch.setattr(runs_module, "GRACE", 0.0)
    monkeypatch.setattr(runs_module, "DEATH_VOTE", 0.2)
    monkeypatch.setattr(brain_module, "DEATH_CARD", 0.05)


@asynccontextmanager
async def brain_with(target: FakeTarget | None = None) -> AsyncGenerator[tuple[Brain, list[dict[str, Any]]]]:
    """A brain on a fake Incus, its overlay messages and Discord posts kept."""
    with Store(":memory:") as store:
        brain = Brain(
            store, lambda: connect_to("127.0.0.1", 1), target or FakeTarget(), target_instance="desk"
        )
        brain.discord = DiscordChat("token", 1, "control", lambda _m: None, lambda: "")
        sent: list[dict[str, Any]] = []
        brain.overlay._send = sent.append  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        now = time.time()
        for name in ("alice", "bob", "carol", "dan"):
            brain.pipeline.presence.saw(f"console:{name}", now - 600)
            brain.pipeline.presence.saw(f"console:{name}", now - 5)
        yield brain, sent


def posted(brain: Brain, channel: str) -> list[str]:
    assert brain.discord is not None
    alerts = brain.discord._alerts  # pyright: ignore[reportPrivateUsage]
    return [text for text, _image, where, _hit in alerts if where == channel]


def kill(brain: Brain, cause: str = "it froze") -> Death:
    brain.runs.grace_until = 0.0
    now = time.time()
    assert brain.runs.observe(None, now - 0.01, quiet=False) is None  # seen well (after any snapshot)
    assert brain.runs.observe(cause, now, quiet=False) is None
    death = brain.runs.observe(cause, now + 0.01, quiet=False)
    assert death is not None
    brain._died(death)  # pyright: ignore[reportPrivateUsage]
    return death


async def verdict(brain: Brain) -> str:
    await asyncio.sleep(0.25)  # the death vote
    result = brain.runs.verdict(time.time())
    assert result is not None
    await brain._after_death_vote(result)  # pyright: ignore[reportPrivateUsage]
    return result


def test_a_death_a_vote_and_a_revive_back_to_the_last_snapshot_for_a_life() -> None:
    async def test() -> None:
        async with brain_with() as (brain, sent):
            assert brain.runs.current is not None and brain.runs.current["number"] == 1
            assert brain.snapshots is not None
            before = await brain.snapshots.take("auto")
            await asyncio.sleep(0.05)  # the machine is seen well after it
            assert await brain.console_message("alice", "!reboot") == ("accepted", None)
            kill(brain)
            assert (
                brain.referee.votes.current is not None and brain.referee.votes.current.phase == "cancelled"
            )
            assert "Chat votes: !revive or !rip." in posted(brain, "ops")[-1]
            assert await brain.console_message("bob", "!revive") == ("accepted", None)
            assert await brain.console_message("carol", "!rip") == ("accepted", None)
            assert await brain.console_message("dan", "!save x") == ("refused", DYING)
            death_view = [m["run"]["death"] for m in sent if m["type"] == "run" and m["run"]["death"]][-1]
            assert (death_view["cause"], death_view["revive"], death_view["rip"]) == ("it froze", 1, 1)
            assert await verdict(brain) == "revive"  # a tie: revive
            target = brain.incus
            assert isinstance(target, FakeTarget) and target.restored == [before.ref]
            assert brain.runs.state == "alive" and brain.runs.lives_left() == 2
            assert (
                "💚 Revived: back to" in posted(brain, "ops")[-1]
                and "2 lives left" in posted(brain, "ops")[-1]
            )

    run(test)


def test_rip_ends_the_run_with_its_card_then_a_new_run_from_the_golden_snapshot() -> None:
    async def test() -> None:
        target = FakeTarget()
        target.there.append("golden")
        async with brain_with(target) as (brain, sent):
            assert brain.snapshots is not None
            old = await brain.snapshots.take("auto")
            brain.store.seen_user("console:alice", "alice", time.time(), frozenset())
            assert await brain.console_message("alice", "!click F7") in (
                ("busy", brain_module.BUSY),
                ("accepted", None),
            )
            kill(brain, "its display died")
            await brain.console_message("bob", "!rip")
            assert await verdict(brain) == "rip"
            cards = [m for m in sent if m["type"] == "card"]
            assert [c["kind"] for c in cards] == ["death", "start"]
            death, start = cards
            assert (death["number"], death["cause"], death["seconds"]) == (1, "its display died", 0.05)
            assert start["number"] == 2 and start["lives"] == 3
            assert target.restored == ["golden"] and old.ref in target.deleted
            assert brain.snapshots.all() == []  # nothing of the dead run to load
            [graveyard] = posted(brain, "graveyard")
            assert graveyard.startswith("💀 **Run #1** died: its display died.\nLived 0 min, 0 lives used.")
            assert "Run #2 begins" in posted(brain, "ops")[-1]
            ended = brain.store.runs()[1]
            assert (ended["number"], ended["cause"]) == (1, "its display died") and ended["ended"]
            assert brain.runs.current is not None and brain.runs.current["number"] == 2
            assert not brain.executor.frozen

    run(test)


def test_a_machine_switched_off_is_switched_on_again_for_the_next_run_and_by_a_revive() -> None:
    """Found in the sandbox (Phase 8 step 4): Incus restores a stopped container's snapshot
    without starting it, and won't restart a stopped one."""

    async def test() -> None:
        target = FakeTarget()
        target.there.append("golden")
        async with brain_with(target) as (brain, _sent):
            assert brain.snapshots is not None
            await brain.snapshots.take("auto")
            await asyncio.sleep(0.05)
            target.running = False  # chat ran poweroff
            kill(brain, "it was switched off")
            assert await verdict(brain) == "revive"  # silence: revive, to the snapshot
            assert target.running and target.starts == 1 and brain.runs.lives_left() == 2
            target.running = False
            kill(brain, "it was switched off")
            assert await brain.console_message("alice", "!rip") == ("accepted", None)
            assert await verdict(brain) == "rip"
            assert target.restored[-1] == "golden" and target.running and target.starts == 2
            assert brain.runs.current is not None and brain.runs.current["number"] == 2

    run(test)


def test_a_revive_without_a_snapshot_switches_a_stopped_machine_on() -> None:
    async def test() -> None:
        target = FakeTarget()
        async with brain_with(target) as (brain, _sent):
            target.running = False
            kill(brain, "it was switched off")
            assert await verdict(brain) == "revive"
            assert target.running and target.starts == 1 and target.restarts == []
            assert "restarted (no snapshot from before)" in posted(brain, "ops")[-1]
            ok, text = await brain._power(force=False)  # pyright: ignore[reportPrivateUsage]
            assert (ok, text, target.restarts) == (True, "rebooted", [False])  # on: a real reboot

    run(test)


def test_after_a_restart_the_overlay_shows_the_run_going_on() -> None:
    """Found on the server: brain restarted on run #1, and the overlay's run panel stayed empty."""
    with Store(":memory:") as store:
        first = Brain(store, lambda: connect_to("127.0.0.1", 1))
        assert first.runs.current is not None
        first.runs.spend_life("rewind")
        again = Brain(store, lambda: connect_to("127.0.0.1", 1))  # the same store: run #1 goes on
        view = again.overlay._run_now()  # pyright: ignore[reportPrivateUsage]
        assert view is not None and (view["number"], view["lives"], view["lives_max"]) == (1, 2, 3)
        assert view["uptime"] >= 0 and view["state"] == "alive"
        assert len(store.runs()) == 1


def test_no_lives_no_vote_and_no_rewinds() -> None:
    async def test() -> None:
        async with brain_with() as (brain, _sent):
            assert brain.snapshots is not None
            await brain.snapshots.take("auto")
            for _ in range(3):
                brain.runs.spend_life("rewind")
            assert await brain.console_message("alice", "!rewind") == ("refused", NO_LIVES)
            death = kill(brain)
            assert death.vote_ends is None and "with no lives left" in posted(brain, "ops")[-1]
            assert await brain.console_message("bob", "!revive") == (
                "refused",
                "the machine isn't dying: nothing to !revive or !rip",
            )
            assert brain.runs.verdict(time.time()) == "rip"

    run(test)


def test_without_a_golden_snapshot_the_next_run_keeps_the_desktop_and_ops_hears_it() -> None:
    async def test() -> None:
        async with brain_with() as (brain, _sent):
            await brain.end_run("ended by a moderator")
            assert "starts on the same desktop" in posted(brain, "ops")[-2]
            assert brain.runs.current is not None and brain.runs.current["number"] == 2

    run(test)


def test_newrun_and_run_from_chat_brain_admin() -> None:
    async def test() -> None:
        target = FakeTarget()
        target.there.append("golden")
        async with brain_with(target) as (brain, _sent):
            ok, text = await brain.admin(["run"])
            assert ok == "ok" and text.startswith("Run #1: 3 of 3 lives left, ")
            assert await brain.console_message("alice", "!newrun") == ("accepted", None)
            for voter in ("bob", "carol", "dan"):
                await brain.console_message(voter, "!yes")
            vote = brain.referee.votes.current
            assert vote is not None
            opened = vote.opened
            for after in (46, 107):  # the window, then the veto countdown
                brain.referee.clock = lambda after=after: opened + after
                brain.referee.tick()
                await asyncio.sleep(0.2)
            assert vote.phase == "done" and vote.result == "a new run begins"
            assert brain.store.runs()[1]["cause"] == "abandoned by vote"
            assert (await brain.admin(["run", "end", "broken", "for", "good"]))[0] == "ok"
            await asyncio.sleep(0.2)
            assert brain.store.runs()[1]["cause"] == "broken for good"
            assert brain.runs.current is not None and brain.runs.current["number"] == 3

    run(test)
