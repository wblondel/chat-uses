"""Chaos modifiers and the points shop (Phase 9 step 4, PLAN.md 6.11)."""

import asyncio
import random
import time
from collections.abc import Callable, Coroutine
from typing import Any, cast

from chatuses.adapters import stream_list_pb2 as pb
from chatuses.adapters.youtube import YouTubeChat
from chatuses.brain import TWISTED, Brain
from chatuses.commands import Click, Drag, MacroRun, Move, TypeText
from chatuses.executor import Job, connect_to
from chatuses.game import chaos as chaos_module
from chatuses.game.chaos import (
    COOLDOWN,
    LAG,
    MAX_QUEUE,
    MODIFIERS,
    RANDOM_EVERY,
    SUPER_CHAT,
    Active,
    Chaos,
)
from chatuses.grid import Grid, ShuffledGrid
from chatuses.pipeline import ChatMessage
from chatuses.store import Store

T = 1_800_000_000.0
GRID = Grid()


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def on(key: str, seed: int = 1) -> Chaos:
    chaos = Chaos(GRID, rng=random.Random(seed))
    chaos.start(key, "alice", "points", T)
    return chaos


# --- what each does -------------------------------------------------------------------------


def test_ten_modifiers_none_in_the_target() -> None:
    assert len(MODIFIERS) == 10
    assert {m.where for m in MODIFIERS.values()} == {"injector", "executor", "overlay"}
    assert all(m.seconds <= 60 and m.cost > 0 for m in MODIFIERS.values())
    assert set(SUPER_CHAT.values()) <= set(MODIFIERS)


def test_keyboards_and_caps_change_what_is_typed() -> None:
    assert on("dvorak").transform(TypeText("hello")) == TypeText("d.nnr")  # QWERTY keys, Dvorak letters
    assert on("dvorak").transform(TypeText("Hi 42!")) == TypeText("Dc 42!")  # only what the layouts differ on
    assert on("azerty").transform(TypeText("quiz")) == TypeText("auiw")
    assert on("caps").transform(TypeText("Hello World")) == TypeText("hELLO wORLD")


def test_mice_change_where_a_click_lands() -> None:
    mirror = on("mirror")
    assert mirror.transform(Click("A1", 50, 50)) == Click("A1", 1549, 849)
    assert mirror.transform(Move("P9", 1550, 850)) == Move("P9", 49, 49)
    assert mirror.transform(Drag("A1", (50, 50), "B2", (150, 150))) == Drag(
        "A1", (1549, 849), "B2", (1449, 749)
    )
    drunk = on("drunk")
    for _ in range(50):
        click = drunk.transform(Click("A1", 5, 5))
        assert isinstance(click, Click) and 0 <= click.x <= 25 and 0 <= click.y <= 25


def test_a_macro_is_changed_step_by_step_and_others_are_left_alone() -> None:
    macro = MacroRun("hi", (TypeText("hello"), Click("A1", 50, 50)))
    assert on("dvorak").transform(macro) == MacroRun("hi", (TypeText("d.nnr"), Click("A1", 50, 50)))
    for key in ("lag", "flip", "quake", "blackout", "shuffle"):
        assert on(key).transform(TypeText("hello")) == TypeText("hello")
    assert on("lag").delay() == LAG and on("quake").delay() == 0


def test_a_shuffled_grid_is_what_chat_aims_with_while_it_lasts() -> None:
    chaos = on("shuffle")
    grid = chaos.aim_grid()
    active = chaos.active
    assert isinstance(grid, ShuffledGrid) and active is not None
    cols, rows = active.labels["cols"], active.labels["rows"]
    assert sorted(cols) == GRID.column_labels and sorted(rows) == GRID.row_labels
    assert grid.locate(cols[0] + rows[0]) == GRID.locate("A1")
    view = chaos.view(T + 1)
    assert view is not None and view["grid"] == {"cols": cols, "rows": rows}
    chaos.tick(T + 60, chance=False)
    assert chaos.aim_grid() is GRID


# --- one at a time ------------------------------------------------------------------------


def test_one_at_a_time_five_minutes_apart() -> None:
    chaos = on("quake")
    assert chaos.why_not("nosuch", T) == "no such chaos: the site's Laws page lists them"
    assert chaos.why_not("flip", T + 5) == "Earthquake is on: 10 s left"
    chaos.tick(T + 15, chance=False)
    assert chaos.active is None
    assert chaos.why_not("flip", T + 16) == "chaos again in 5 min"
    assert chaos.why_not("flip", T + 15 + COOLDOWN) is None
    assert chaos.stop(T) is False


def test_paid_ones_wait_their_turn_without_a_cooldown() -> None:
    chaos = Chaos(GRID)
    assert chaos.paid("quake", "fan1", T) and chaos.active is not None
    assert chaos.paid("flip", "fan2", T)
    for i in range(MAX_QUEUE - 1):
        assert chaos.paid("caps", f"fan{i + 3}", T)
    assert not chaos.paid("caps", "late", T)  # the queue is full
    chaos.tick(T + 15, chance=False)
    active = chaos.active
    assert active is not None and (active.modifier.key, active.by, active.source) == (
        "flip",
        "fan2",
        "superchat",
    )


def test_chance_brings_one_now_and_then_when_brain_allows() -> None:
    chaos = Chaos(GRID, rng=random.Random(3))
    chaos.tick(T, chance=True)
    chaos.tick(T + RANDOM_EVERY - 1, chance=True)
    assert chaos.active is None  # not before its time
    started: list[Active] = []
    for i in range(1, 20):
        chaos.tick(T + i * RANDOM_EVERY, chance=True)
        if chaos.active is not None:
            started.append(chaos.active)
            chaos.stop(T + i * RANDOM_EVERY)
            chaos.free_at = 0
    assert started and all(a.source == "random" and a.by == "chance" for a in started)
    never = Chaos(GRID, rng=random.Random(3))
    for i in range(20):
        never.tick(T + i * RANDOM_EVERY, chance=False)
    assert never.active is None


def test_points_buy_one_and_are_spent() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "a", T, frozenset())
        store.add_points(["yt:a"], 50)
        chaos = Chaos(GRID)
        assert chaos_module.buy(chaos, store, "yt:a", "a", "mirror", T) == (
            False,
            "Inverted mouse costs 80 points: you have 50",
        )
        assert chaos_module.buy(chaos, store, "yt:a", "a", "quake", T) == (True, "Earthquake for 15 s")
        assert store.standing("yt:a")[0] == 20
        assert chaos_module.buy(chaos, store, "yt:a", "a", "caps", T)[1] == "Earthquake is on: 15 s left"
        assert store.standing("yt:a")[0] == 20  # nothing spent on a refusal


# --- through brain -------------------------------------------------------------------------


def brain_on(store: Store) -> Brain:
    brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
    brain.executor.online.set()
    sent: list[dict[str, Any]] = []
    brain.overlay._send = sent.append  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    brain.__dict__["sent"] = sent
    for user in ("console:alice", "console:bob"):
        store.seen_user(user, user, time.time(), frozenset())
    return brain


def queued(brain: Brain) -> list[Job]:
    jobs: list[Job] = []
    while not brain.executor.queue.empty():
        jobs.append(brain.executor.queue.get_nowait())
    return jobs


def test_brain_sells_chaos_shows_it_and_runs_commands_through_it() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            store.add_points(["console:alice"], 100)
            assert await brain.console_message("alice", "!buy mirror") == ("accepted", None)
            sent = cast(list[dict[str, Any]], brain.__dict__["sent"])
            view = [m for m in sent if m["type"] == "chaos"][-1]["chaos"]
            assert (view["key"], view["by"], view["source"]) == ("mirror", "alice", "points")
            assert "Chaos: Inverted mouse (by alice)" in brain.status_text()
            assert await brain.console_message("bob", "!click A1") == ("accepted", None)
            [job] = queued(brain)
            assert job.command == Click("A1", 1549, 849)
            line = [m for m in sent if m["type"] == "log" and m.get("text") == "!click A1"]
            assert line  # the overlay shows what chat typed
            assert await brain.console_message("bob", "!buy quake") == (
                "refused",
                f"Inverted mouse is on: {MODIFIERS['mirror'].seconds} s left",
            )
            brain.moderation.panic("console", "test")
            brain.chaos_tick(time.time())
            assert brain.chaos.active is None  # a panic stops it

    run(test)


def test_a_keyboard_remap_can_t_type_what_the_filter_refuses() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            brain.filter.typed_kind = lambda text: "hate" if "d.nnr" in text else None  # type: ignore[method-assign]
            assert (await brain.admin(["chaos", "start", "dvorak"]))[0] == "ok"
            assert await brain.console_message("bob", "!type hello") == ("refused", TWISTED)
            assert await brain.console_message("bob", "!type hi") == ("accepted", None)
            assert [job.command for job in queued(brain)] == [TypeText("dc")]

    run(test)


def test_a_shuffled_grid_through_brain_and_the_admin_s_switches() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            assert (await brain.admin(["chaos", "start", "shuffle"]))[0] == "ok"
            active = brain.chaos.active
            assert active is not None
            shown = active.labels["cols"][0] + active.labels["rows"][0]
            assert await brain.console_message("bob", f"!click {shown}") == ("accepted", None)
            [job] = queued(brain)
            assert isinstance(job.command, Click) and (job.command.x, job.command.y) == (50, 50)
            assert await brain.admin(["chaos", "stop"]) == ("ok", "Chaos stopped.")
            assert brain.pipeline.grid.locate("A1") == (50, 50)
            assert (await brain.admin(["chaos"]))[1].startswith("Chaos: none")
            assert await brain.admin(["chaos", "stop"]) == ("error", "no chaos on")
            brain.referee.ruleset = brain.referee.ruleset.__class__(
                brain.referee.ruleset.name, features=frozenset({"anarchy"})
            )
            store.add_points(["console:alice"], 100)
            assert await brain.console_message("alice", "!buy quake") == (
                "refused",
                "chaos is off in this ruleset",
            )

    run(test)


def test_a_super_chat_s_tier_brings_chaos_never_its_amount_or_text() -> None:
    got: list[tuple[str, str, int]] = []
    delivered: list[ChatMessage] = []
    chat = YouTubeChat("key", delivered.append, on_super_chat=lambda u, n, t: got.append((u, n, t)))
    item = pb.LiveChatMessage(id="sc1")
    item.snippet.type = pb.LiveChatMessageSnippet.TypeWrapper.SUPER_CHAT_EVENT
    item.snippet.super_chat_details.tier = 2
    item.snippet.super_chat_details.amount_micros = 5_000_000
    item.snippet.display_message = "!rewind please"
    item.author_details.channel_id = "UCfan"
    item.author_details.display_name = "Fan"
    chat._item(item)  # pyright: ignore[reportPrivateUsage]
    assert got == [("yt:UCfan", "Fan", 2)] and delivered == []  # not a chat command, not a vote

    with Store(":memory:") as store:
        brain = brain_on(store)
        brain.super_chat("yt:UCfan", "Fan", 2)
        brain.super_chat("yt:UCbig", "Big", 9)
        active = brain.chaos.active
        assert active is not None and (active.modifier.key, active.by) == (SUPER_CHAT[2], "Fan")
        assert [m.key for m, _by in brain.chaos.queue] == [SUPER_CHAT[max(SUPER_CHAT)]]
        assert "1 Super Chats waiting" not in brain.chaos_line()  # one's on: the line says which
