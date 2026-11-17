"""Macros (Phase 9 step 1, PLAN.md 6.10): chat's own commands, voted in and kept across runs."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fake_vnc import FakeVnc
from test_pipeline import Stub, msg

from chatuses.brain import MAX_MACROS, Brain
from chatuses.commands import (
    MacroDefine,
    MacroRemove,
    MacroRun,
    ParseError,
    PressKeys,
    TypeText,
    describe,
    describe_stored,
    macro_source,
)
from chatuses.executor import connect_to, execute
from chatuses.grid import Grid
from chatuses.parser import MAX_STEPS, parse
from chatuses.pipeline import Pipeline
from chatuses.safety.textfilter import HATE
from chatuses.site import Site, gather
from chatuses.store import Store, to_json

GRID = Grid()
REPO = Path(__file__).resolve().parents[2]


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def defined(text: str) -> MacroDefine:
    parsed = parse(text, GRID)
    assert isinstance(parsed, MacroDefine), parsed
    return parsed


# --- the grammar --------------------------------------------------------------------------


def test_a_macro_is_input_commands_with_or_without_their_bang() -> None:
    macro = defined("!macro Hello = type hello world; !enter;key ctrl+s")
    assert macro.name == "hello"
    assert macro.steps[0] == TypeText("hello world") and isinstance(macro.steps[2], PressKeys)
    assert describe(macro) == "!macro hello = type hello world; enter; key ctrl+s"
    assert defined(f"!macro hello = {macro_source(macro.steps)}") == macro  # the source parses back
    run_it = parse("!hello", GRID, {"hello": macro.steps})
    assert isinstance(run_it, MacroRun) and run_it == MacroRun("hello", macro.steps)
    assert describe(run_it) == "!hello"
    assert parse("!HELLO", GRID, {"hello": macro.steps}) == run_it
    assert parse("!unmacro Hello", GRID) == MacroRemove("hello")
    assert parse("!hello", GRID) == ParseError("unknown command !hello (try !help)")  # not voted in


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("!macro", "needs a name and its commands"),
        ("!macro term", "needs a name and its commands"),
        ("!macro term =", "needs a name and its commands"),
        ("!macro t = enter", "2 to 12 letters and digits"),
        ("!macro 9lives = enter", "2 to 12 letters and digits"),
        ("!macro waytoolongname = enter", "2 to 12 letters and digits"),
        ("!macro click = enter", "!click is already a command"),
        ("!macro rewind = enter", "!rewind is already a command"),
        ("!macro amend = enter", "!amend is already a command"),  # coming in Phase 9
        ("!macro buy = enter", "!buy is already a command"),
        ("!macro ab = enter;; enter", "separated by ; with none empty"),
        ("!macro ab = " + "; ".join(["enter"] * (MAX_STEPS + 1)), f"at most {MAX_STEPS} commands"),
        ("!macro ab = rewind", "input commands only"),
        ("!macro ab = macro cd = enter", "input commands only"),
        ("!macro ab = click Z99", "the macro's command 1: "),
        ("!macro ab = enter; type", "the macro's command 2: !type needs some text"),
    ],
)
def test_bad_macros_are_refused_with_the_reason(text: str, why: str) -> None:
    parsed = parse(text, GRID)
    assert isinstance(parsed, ParseError) and why in parsed.reason, parsed


def test_no_macro_inside_a_macro_and_a_run_takes_nothing() -> None:
    macros = {"hi": (TypeText("hi"),)}
    assert isinstance(parse("!macro ho = hi", GRID, macros), ParseError)  # steps never see macros
    assert parse("!hi now", GRID, macros) == ParseError("!hi is a macro: it takes nothing after it")


# --- the filter -----------------------------------------------------------------------


def test_a_macro_s_name_and_text_are_filtered_when_proposed_and_when_run() -> None:
    stub = Stub()
    macros: dict[str, Any] = {"hi": (TypeText("bad"),), "ok": (TypeText("ab"), PressKeys(()))}
    with Store(":memory:") as store:
        check_filter(Pipeline(store, censor=stub, macros=lambda: macros), stub)


def check_filter(pipe: Pipeline, stub: Stub) -> None:
    assert pipe.handle(msg("!macro badname = enter", user="yt:a")).kind == "filtered"
    assert pipe.handle(msg("!macro ab = type a bad word", user="yt:b")).reason == HATE
    assert pipe.handle(msg("!macro ab = type fine; enter", user="yt:c")).kind == "accepted"
    assert pipe.handle(msg("!hi", user="yt:d")).kind == "filtered"  # chat can't launder text through one
    assert pipe.handle(msg("!ok", 10.0, user="yt:e")).kind == "accepted"
    assert stub.seen == ["typed ab"]  # the filter learns what a macro typed


# --- running one ----------------------------------------------------------------------


def test_a_macro_runs_its_steps_in_order_as_one_command() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        vnc = await connect_to("127.0.0.1", server.port)
        steps = defined("!macro hi = type hi; enter; click A1").steps
        await execute(vnc, MacroRun("hi", steps), GRID)
        await vnc.close()
        events = list(await server.wait_hung_up())
        await server.stop()
        typed = [("key", d, ord(c)) for c in "hi" for d in (True, False)]
        assert events == [
            *typed,
            ("key", True, 0xFF0D),
            ("key", False, 0xFF0D),
            ("ptr", 0, 50, 50),
            ("ptr", 1, 50, 50),
            ("ptr", 0, 50, 50),
        ]

    run(test)


def test_a_macro_run_is_stored_and_described_by_its_name() -> None:
    steps = defined("!macro hi = type hi").steps
    import json

    assert describe_stored(json.loads(to_json(MacroRun("hi", steps)))) == "!hi"


# --- through brain ----------------------------------------------------------------------


def brain_on(store: Store) -> Brain:
    brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
    now = time.time()
    for name in ("alice", "bob", "carol", "dan"):
        brain.pipeline.presence.saw(f"console:{name}", now - 600)
        brain.pipeline.presence.saw(f"console:{name}", now - 5)
    return brain


async def vote(brain: Brain, text: str) -> str:
    """alice proposes, three more say yes, the window closes; how it ended."""
    assert await brain.console_message("alice", text) == ("accepted", None), text
    for voter in ("bob", "carol", "dan"):
        await brain.console_message(voter, "!yes")
    current = brain.referee.votes.current
    assert current is not None
    opened = current.opened
    brain.referee.clock = lambda: opened + 31
    brain.referee.tick()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if current.phase in ("done", "error", "failed"):
            break
    brain.referee.clock = time.time
    return f"{current.phase}: {current.result}"


def test_chat_votes_a_macro_in_uses_it_and_votes_it_out() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            assert await vote(brain, "!macro hi = type hello; enter") == (
                "done: !hi now does: type hello; enter"
            )
            assert [m["name"] for m in store.macros()] == ["hi"]
            assert store.macros()[0]["by"] == "console:alice"
            outcome = await brain.console_message("bob", "!hi")
            assert outcome in (("accepted", None), ("busy", brain_module_busy()))
            assert "!hi = type hello; enter" in (await brain.admin(["macros"]))[1]
            again = brain_on(store)  # brain restarts: the macro is still there
            assert again.macros == brain.macros
            assert (await brain.console_message("carol", "!macro hi = enter")) == (
                "refused",
                "!hi is a macro already: !unmacro hi first",
            )
            assert await vote(brain, "!unmacro hi") == "done: !hi is gone"
            assert brain.macros == {} and store.macros() == []
            assert await brain.console_message("dan", "!unmacro hi") == ("refused", "there's no macro !hi")

    run(test)


def brain_module_busy() -> str:
    from chatuses.brain import BUSY

    return BUSY


def test_at_most_64_macros_and_moderators_remove_one_without_a_vote() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            for i in range(MAX_MACROS):
                brain.macros[f"m{i}"] = (TypeText("x"),)
            assert await brain.console_message("alice", "!macro extra = enter") == (
                "refused",
                f"chat has {MAX_MACROS} macros already: !unmacro one first",
            )
            store.add_macro(time.time(), "gone", "type x", "console:alice")
            brain.macros["gone"] = (TypeText("x"),)
            assert await brain.admin(["macro", "remove", "!Gone"]) == ("ok", "Macro !gone removed.")
            assert "gone" not in brain.macros and store.macros() == []
            assert await brain.admin(["macro", "remove", "nope"]) == ("error", "no macro !nope")

    run(test)


def test_a_deletion_request_takes_the_proposer_off_a_macro_not_the_macro() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "a", time.time(), frozenset())
        store.add_macro(time.time(), "hi", "type hi", "yt:a")
        store.forget(time.time(), "yt:a")
        assert [(m["name"], m["by"]) for m in store.macros()] == [("hi", "")]


def test_a_macro_that_no_longer_parses_is_left_out_at_start() -> None:
    with Store(":memory:") as store:
        store.add_macro(time.time(), "hi", "type hi", "")
        store.add_macro(time.time(), "old", "click Z99", "")  # a grid that changed, say
        assert list(brain_on(store).macros) == ["hi"]


def test_the_site_lists_chat_s_macros() -> None:
    with Store(":memory:") as store:
        store.add_macro(1_800_000_000, "term", "key ctrl+alt+t", "")
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
        assert "<code>!term</code></td><td><code>key ctrl+alt+t</code>" in page
        assert "<code>!macro</code>" in page and "<code>!unmacro</code>" in page  # their votes' rules


def test_a_refusal_that_quotes_chat_is_screened_before_the_stream() -> None:
    """ "unknown command !word" quotes chat's word: a slur there never reaches the overlay."""

    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store)
            sent: list[dict[str, Any]] = []
            brain.overlay._send = sent.append  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
            brain.filter.kind = lambda text: "hate" if "zzslur" in text else None  # type: ignore[method-assign]
            await brain.console_message("alice", "!zzslur")
            await brain.console_message("bob", "!nosuch")
            reasons = [m["reason"] for m in sent if m["type"] == "log" and m["state"] == "refused"]
            assert reasons == ["that's not a command (try !help)", "unknown command !nosuch (try !help)"]

    run(test)
