import asyncio
import json
import os
import socket
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY

import aiohttp
import discord
import pytest
from aiohttp import WSMsgType, test_utils
from fake_incus import FakeTarget
from fake_vnc import FakeVnc

from chatuses import brain as brain_module
from chatuses import console, executor
from chatuses.adapters.discord_bot import DiscordChat
from chatuses.adapters.youtube import YouTubeChat
from chatuses.brain import Brain, Config
from chatuses.executor import connect_to
from chatuses.incus import Incus, IncusError
from chatuses.pipeline import PAUSED, ChatMessage
from chatuses.referee.votes import Vote
from chatuses.safety.screen_ocr import Hit
from chatuses.safety.textfilter import HATE, LISTS
from chatuses.screen import Frame
from chatuses.snapshots import Snapshots
from chatuses.store import Store


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "RECONNECT", (0.01, 0.05))


class Feed:
    """An overlay page's view of brain's feed."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    async def next(self, kind: str | None = None) -> dict[str, Any]:
        while True:
            msg = await asyncio.wait_for(self.ws.receive(), 2)
            assert msg.type == WSMsgType.TEXT, msg
            data: dict[str, Any] = json.loads(msg.data)
            if kind is None or data["type"] == kind:
                return data


@asynccontextmanager
async def running(
    server: FakeVnc | None = None, incus: Incus | None = None, thumbs: Path | None = None
) -> AsyncGenerator[tuple[Brain, Feed]]:
    """A brain with its executor running, and an overlay page connected to it."""
    port = server.port if server else 1  # port 1: nothing listens, the target stays offline
    with Store(":memory:") as store:
        brain = Brain(store, lambda: connect_to("127.0.0.1", port), incus, thumbs=thumbs)
        task = asyncio.create_task(brain.executor.run())
        async with test_utils.TestClient(test_utils.TestServer(brain.web_app())) as client:
            ws = await client.ws_connect("/ws/overlay")
            feed = Feed(ws)
            await feed.next("hello")
            if server:
                async with asyncio.timeout(2):
                    await brain.executor.online.wait()
                assert await feed.next() == {"type": "status", "target": True}
            yield brain, feed
            await ws.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_a_click_from_chat_reaches_the_desktop_and_the_overlay() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        async with running(server) as (brain, feed):
            assert await brain.console_message("alice", "!click F7") == ("accepted", None)
            line = await feed.next("log")
            assert (line["user"], line["platform"], line["text"], line["state"]) == (
                "alice",
                "console",
                "!click F7",
                "queued",
            )
            assert await feed.next() == {"type": "log-state", "id": line["id"], "state": "done"}
            assert await feed.next() == {"type": "pointer", "x": 550, "y": 650, "user": "alice"}
            assert await server.wait_for(3) == [
                ("ptr", 0, 550, 650),
                ("ptr", 1, 550, 650),
                ("ptr", 0, 550, 650),
            ]
            [*_, executed] = brain.store.events()
            assert executed[2] == "executed" and executed[3]["result"] == "done"
        await server.stop()

    run(test)


def test_typed_text_is_shown_as_parsed() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        async with running(server) as (brain, feed):
            await brain.console_message("bob", "!TYPE   hi")
            assert (await feed.next("log"))["text"] == "!type hi"
            assert (await feed.next("log-state"))["state"] == "done"
        await server.stop()

    run(test)


def test_refusals_show_a_fixed_reason_once_per_user_per_10_seconds() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            assert (await brain.console_message("troll", "!click xyzzy"))[0] == "invalid"
            line = await feed.next("log")
            assert (line["text"], line["state"]) == (None, "refused")
            assert line["reason"] and "xyzzy" not in line["reason"]
            assert (await brain.console_message("troll", "!click xyzzy"))[0] == "invalid"  # not shown again
            await brain.console_message("other", "!nope")
            assert (await feed.next("log"))["user"] == "other"

    run(test)


def test_commands_while_the_target_is_offline_are_refused_as_busy() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            assert await brain.console_message("alice", "!enter") == ("busy", brain_module.BUSY)
            line = await feed.next("log")
            assert (line["text"], line["state"], line["reason"]) == (None, "refused", brain_module.BUSY)
            assert await brain.console_message("alice", "!enter") == (
                "busy",
                brain_module.BUSY,
            )  # not shown again
            await brain.console_message("bob", "!grid")
            assert (await feed.next("log"))["user"] == "bob"
            [*_, busy] = [e for e in brain.store.events() if e[2] == "executed"]
            assert busy[3]["result"] == "busy" and '"PressKeys"' in busy[3]["command"]

    run(test)


def test_meta_commands() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            await brain.console_message("alice", "!grid")
            assert (await feed.next("log"))["state"] == "done"
            assert await feed.next() == {"type": "grid"}
            newcomer = "you can push the meter once you've been here 2 minutes"
            assert await brain.console_message("alice", "!democracy") == ("refused", newcomer)
            line = await feed.next("log")
            assert (line["text"], line["state"], line["reason"]) == (None, "refused", newcomer)

    run(test)


def settle(brain: Brain, *names: str) -> None:
    """Console users who've been around for 10 minutes and spoke a moment ago (PLAN.md 6.5)."""
    now = time.time()
    for name in names:
        brain.pipeline.presence.saw(f"console:{name}", now - 600)
        brain.pipeline.presence.saw(f"console:{name}", now - 5)


def snaps(brain: Brain) -> Snapshots:
    assert brain.snapshots is not None
    return brain.snapshots


def vote_now(brain: Brain) -> Vote:
    assert brain.referee.votes.current is not None
    return brain.referee.votes.current


def test_a_power_vote_on_the_overlay() -> None:
    async def test() -> None:
        target = FakeTarget()
        async with running(incus=target) as (brain, feed):
            assert await brain.console_message("newbie", "!reboot") == (
                "refused",
                "you can propose votes once you've been here 2 minutes",
            )
            settle(brain, "alice", "bob", "carol")
            assert await brain.console_message("alice", "!reboot") == ("accepted", None)
            vote = (await feed.next("vote"))["vote"]
            assert (vote["text"], vote["by"], vote["phase"], vote["yes"], vote["quorum"]) == (
                "!reboot",
                "alice",
                "voting",
                1,
                2,
            )
            assert 29 < vote["ends_in"] <= 30
            proposed = await feed.next()  # the proposal goes in the log...
            assert (proposed["type"], proposed["text"], proposed["state"]) == ("log", "!reboot", "done")
            assert await brain.console_message("bob", "!yes") == ("accepted", None)
            assert await feed.next() == {
                "type": "vote",
                "vote": {**vote, "yes": 2, "ends_in": ANY},
            }  # no log line
            assert await brain.console_message("carol", "!rewind") == (
                "refused",
                "a vote is already running: !yes or !no",
            )
            now = time.time()
            brain.referee.clock = lambda: now + 31  # the window is over
            brain.referee.tick()
            assert (await feed.next("vote"))["vote"]["phase"] == "running"
            ended = (await feed.next("vote"))["vote"]
            assert (ended["phase"], ended["result"]) == ("done", "rebooted")
            assert target.restarts == [False]  # graceful
            [row] = brain.store.votes()
            assert (row["kind"], row["proposer"], row["result"], row["tally"]["yes"]) == (
                "reboot",
                "console:alice",
                "done",
                2,
            )
            assert (
                "Ruleset: roguelike; mode: anarchy (meter 50); vote: !reboot: done, rebooted"
                in brain.status_text()
            )
            assert not brain.executor.frozen  # input is back after the reboot

    run(test)


def test_a_panic_ends_the_vote_and_moderators_can_too() -> None:
    async def test() -> None:
        async with running(incus=FakeTarget()) as (brain, feed):
            settle(brain, "alice")
            await brain.console_message("alice", "!reset")
            assert (await feed.next("vote"))["vote"]["phase"] == "voting"
            brain.moderation.panic("console", "test")
            ended = (await feed.next("vote"))["vote"]
            assert (ended["phase"], ended["result"]) == ("cancelled", "cancelled: the stream is paused")
            assert await brain.console_message("alice", "!reboot") == ("refused", "the machine is paused")
            brain.moderation.resume("console")
            assert await brain.admin(["endvote"]) == ("error", "No vote is running.")
            await brain.console_message("alice", "!reboot")
            assert await brain.admin(["endvote"]) == ("ok", "Vote ended: !reboot (ended by a moderator).")

    run(test)


def test_time_travel_votes_save_rewind_and_load() -> None:
    async def test() -> None:
        target = FakeTarget()
        async with running(incus=target) as (brain, feed):
            settle(brain, "alice", "bob", "carol", "dan")
            assert await brain.console_message("alice", "!rewind") == (
                "refused",
                "no snapshot to go back to yet",
            )
            assert await brain.console_message("alice", "!load clean") == ("refused", "no save called clean")
            assert (await brain.admin(["snapshot"]))[0] == "ok"  # an operator takes one
            timeline = await feed.next("timeline")
            latest = snaps(brain).auto(1)
            assert latest and [s["label"] for s in timeline["timeline"]] == [latest.label]
            assert await brain.console_message("alice", "!rewind 2") == (
                "refused",
                "only 1 snapshots to go back to",
            )

            async def vote(text: str, voters: tuple[str, ...] = ("bob", "carol", "dan")) -> dict[str, Any]:
                assert await brain.console_message("alice", text) == ("accepted", None)
                for voter in voters:
                    await brain.console_message(voter, "!yes")
                start = vote_now(brain).opened
                for after in (31, 62):  # the window, then the veto countdown if there's one
                    brain.referee.clock = lambda after=after: start + after
                    brain.referee.tick()
                    await asyncio.sleep(0.05)  # the action runs as a task
                    if brain.referee.votes.current and brain.referee.votes.current.phase != "veto":
                        break
                current = brain.referee.votes.current
                assert current is not None
                return {"phase": current.phase, "result": current.result}

            assert await vote("!save clean") == {"phase": "done", "result": "saved as clean"}
            saves = (await feed.next("timeline"))["saves"]
            assert [s["label"] for s in saves] == ["clean"]
            brain.referee.votes._cooldown.clear()  # pyright: ignore[reportPrivateUsage]
            first = snaps(brain).auto(1)
            assert first
            assert await vote("!rewind") == {
                "phase": "done",
                "result": f"back to {first.label} (2 lives left)",
            }
            assert target.restored == [first.ref] and not brain.executor.frozen
            brain.referee.votes._cooldown.clear()  # pyright: ignore[reportPrivateUsage]
            save = snaps(brain).named("clean")
            assert save
            assert await vote("!load clean") == {"phase": "done", "result": "back to clean (1 life left)"}
            assert target.restored[-1] == save.ref
            assert [p["kind"] for _, _, _, p in brain.store.recent_events(("time-travel",))] == [
                "named",
                "auto",
            ]

    run(test)


def test_a_reboot_the_machine_ignores_says_to_reset() -> None:
    async def test() -> None:
        target = FakeTarget()
        target.ignores_shutdown = True
        async with running(incus=target) as (brain, _feed):
            settle(brain, "alice", "bob")
            await brain.console_message("alice", "!reboot")
            await brain.console_message("bob", "!yes")
            start = vote_now(brain).opened
            brain.referee.clock = lambda: start + 31
            brain.referee.tick()
            await asyncio.sleep(0.05)
            current = brain.referee.votes.current
            assert current and (current.phase, current.result) == ("error", brain_module.NO_REBOOT)
            assert not brain.executor.frozen

    run(test)


def test_without_incus_there_is_no_time_travel() -> None:
    async def test() -> None:
        async with running() as (brain, _feed):
            settle(brain, "alice")
            for text in ("!rewind", "!save x", "!reboot"):
                assert await brain.console_message("alice", text) == ("refused", brain_module.NO_INCUS)
            assert await brain.admin(["snapshots"]) == ("error", brain_module.NO_INCUS)
            assert "Snapshots: off (not set up)" in brain.status_text()

    run(test)


def test_a_moderator_rewinds_to_any_snapshot() -> None:
    async def test() -> None:
        target = FakeTarget()
        async with running(incus=target) as (brain, _feed):
            await brain.admin(["snapshot"])
            settle(brain, "alice")
            await brain.console_message("alice", "!reboot")  # an open vote ends
            assert await brain.admin(["rewind-to", "nope"]) == (
                "error",
                "No snapshot 'nope': the dashboard (or chat-brain-admin snapshots) lists them.",
            )
            snap = snaps(brain).auto(1)
            assert snap
            assert await brain.admin(["rewind-to", "1"]) == ("ok", f"Rewinding to #1 ({snap.label})…")
            await asyncio.sleep(0.05)
            assert target.restored == [snap.ref]
            assert brain.referee.votes.current and brain.referee.votes.current.phase == "cancelled"
            assert (await brain.admin(["snapshots"]))[1].startswith(f"#1 auto {snap.label}")

    run(test)


def test_auto_snapshots_when_due_and_something_changed() -> None:
    async def test() -> None:
        async with running(incus=FakeTarget()) as (brain, _feed):
            start = brain._started  # pyright: ignore[reportPrivateUsage]
            brain.executor.online.set()  # as if the desktop were connected
            due = start + 15 * 60 + 1
            assert not brain._auto_due(start + 60)  # pyright: ignore[reportPrivateUsage]
            assert not brain._auto_due(due)  # pyright: ignore[reportPrivateUsage]  # nothing changed
            brain.executor.last_input = start + 100
            assert brain._auto_due(due)  # pyright: ignore[reportPrivateUsage]
            brain.moderation.panic("console")
            assert not brain._auto_due(due)  # pyright: ignore[reportPrivateUsage]
            brain.moderation.resume("console")
            snap = await snaps(brain).take("auto")
            assert not brain._auto_due(snap.created + 60)  # pyright: ignore[reportPrivateUsage]
            brain.executor.last_input = snap.created + 30
            assert brain._auto_due(snap.created + 15 * 60 + 1)  # pyright: ignore[reportPrivateUsage]

    run(test)


def test_thumbnails_are_served_and_nothing_else(tmp_path: Path) -> None:
    async def test() -> None:
        (tmp_path / "7.png").write_bytes(b"\x89PNG fake")
        (tmp_path / "secret.txt").write_text("no")
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1), thumbs=tmp_path)
            async with test_utils.TestClient(test_utils.TestServer(brain.web_app())) as client:
                resp = await client.get("/thumbs/7.png")
                assert resp.status == 200 and resp.content_type == "image/png"
                assert await resp.read() == b"\x89PNG fake"
                for path in (
                    "/thumbs/8.png",
                    "/thumbs/secret.txt",
                    "/thumbs/..%2Fsecret.txt",
                    "/thumbs/x.png",
                ):
                    assert (await client.get(path)).status == 404, path

    run(test)


def test_democracy_runs_the_most_voted_command() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        async with running(server) as (brain, feed):
            settle(brain, "alice", "bob", "carol")
            assert await brain.console_message("alice", "!democracy") == ("accepted", None)
            mode = await feed.next("mode")
            assert (mode["mode"], mode["meter"]) == ("anarchy", 52)
            assert await brain.admin(["mode", "democracy"]) == ("ok", "Mode: democracy (locked for 120 s).")
            assert (await feed.next("mode"))["mode"] == "democracy"
            for name, text in (("alice", "!click F7"), ("bob", "!click F7"), ("carol", "!click A1")):
                assert await brain.console_message(name, text) == ("accepted", None)
            tally = (await feed.next("mode"))["tally"]
            while tally["total"] < 3:
                tally = (await feed.next("mode"))["tally"]
            assert tally["options"] == [{"text": "!click F7", "votes": 2}, {"text": "!click A1", "votes": 1}]
            assert brain.executor.queue.qsize() == 0  # ballots, not inputs
            now = time.time()
            brain.referee.clock = lambda: now + 13
            brain.referee.tick()
            line = await feed.next("log")
            assert (line["user"], line["platform"], line["text"], line["state"]) == (
                "democracy (2/3)", "democracy", "!click F7", "queued"
            )  # fmt: skip
            assert await feed.next("log-state") == {"type": "log-state", "id": line["id"], "state": "done"}
            assert (await server.wait_for(3))[0] == ("ptr", 0, 550, 650)  # F7
            assert await brain.admin(["mode", "democracy"]) == ("error", "It's already democracy.")
            assert "mode: democracy" in brain.status_text()
        await server.stop()

    run(test)


def test_discord_panel_presses_and_what_the_panel_shows() -> None:
    async def test() -> None:
        async with running(incus=FakeTarget()) as (brain, feed):
            state = brain.panel_state()
            assert (state.mode, state.meter, state.vote, state.democracy, state.votes, state.paused) == (
                "anarchy", 50, None, True, True, False
            )  # fmt: skip
            settle(brain, "alice", "bob")
            now = time.time()
            brain.pipeline.presence.saw("dc:7", now - 600)
            brain.pipeline.presence.saw("dc:7", now - 5)

            def press(text: str, user: str = "dc:7") -> str:
                return brain.press(ChatMessage(user, "Mia", text, time.time(), frozenset()))

            assert press("!yes") == "No vote running."
            await brain.console_message("other", "!nope")
            assert (await feed.next("log"))["user"] == "other"  # the press's refusal wasn't on stream
            assert await brain.console_message("alice", "!reboot") == ("accepted", None)
            vote = vote_now(brain)
            state = brain.panel_state()
            assert state.vote is not None and state.vote["phase"] == "voting" and state.vote_ends == vote.ends
            assert press("!yes") == "You voted yes." and vote.ballots["dc:7"] is True
            assert press("!no") == "You voted no." and vote.ballots["dc:7"] is False
            assert (
                press("!yes", "dc:8") == "Votes count from people who were here 2 minutes before it opened."
            )
            assert press("!democracy") == "You pushed the meter toward democracy."
            assert brain.panel_state().meter == 52
            assert press("!democracy") == "One push every 10 s."
            brain.moderation.panic("test", "")
            assert brain.panel_state().paused

    run(test)


def test_rulesets_and_laws_from_the_admin_console(tmp_path: Path) -> None:
    async def test() -> None:
        rulesets = Path(__file__).resolve().parents[2] / "rulesets"
        (tmp_path / "calm.yaml").write_text("commands: {disabled: [reboot]}\n")
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1), rulesets=rulesets)
            assert await brain.admin(["ruleset"]) == ("ok", "Ruleset: roguelike (available: roguelike)")
            assert await brain.admin(["ruleset", "nope"]) == (
                "error",
                f"Ruleset unchanged: no ruleset 'nope' in {rulesets}",
            )
            assert await brain.admin(["laws"]) == ("ok", "All laws are the ruleset's (roguelike).")
            assert (await brain.admin(["law", "tiers.reboot.window_s", "45"]))[0] == "ok"
            assert brain.referee.laws.tier("reboot").window_s == 45
            assert await brain.admin(["law", "auto_snapshot_min", "5"]) == (
                "error",
                "Law unchanged: auto_snapshot_min: 5 is out of bounds (10 to 30)",
            )
            assert await brain.admin(["laws"]) == ("ok", "tiers.reboot.window_s = 45")
            assert await brain.admin(["law", "tiers.reboot.window_s", "default"]) == (
                "ok",
                "tiers.reboot.window_s: back to the ruleset's",
            )
            assert brain.referee.laws.tier("reboot").window_s == 30
            brain.referee.rulesets = tmp_path
            assert await brain.admin(["ruleset", "calm"]) == ("ok", "Ruleset: calm")
            assert store.setting("ruleset") == "calm"
            settle(brain, "alice")
            assert await brain.console_message("alice", "!reboot") == (
                "refused",
                "!reboot is off in this ruleset",
            )

    run(test)


def test_ordinary_chat_stays_off_the_overlay_and_names_are_cleaned() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            assert (await brain.console_message("alice", "hello everyone"))[0] == "chat"
            assert await brain.console_message("a:b", "!grid") == ("error", "bad name")
            await brain.console_message("x" * 40, "!grid")
            assert (await feed.next("log"))["user"] == "x" * 23 + "…"

    run(test)


def test_brain_runs_and_stops(tmp_path: Path) -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        sock = tmp_path / "console.sock"
        config = Config("127.0.0.1", server.port, bind="127.0.0.1", port=0, db=":memory:", console=str(sock))
        stop = asyncio.Event()
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", server.port))
            task = asyncio.create_task(brain.serve(config, stop))
            async with asyncio.timeout(2):
                await brain.executor.online.wait()
            assert await console.say(sock, "alice", "!enter") == {"outcome": "accepted", "reason": None}
            assert oct(sock.stat().st_mode & 0o777) == "0o600"
            stop.set()
            await task
        assert await server.wait_hung_up()  # the executor let go of the desktop
        await server.stop()

    run(test)


def test_console_rejects_bad_requests(tmp_path: Path) -> None:
    async def handle(_name: str, _text: str) -> tuple[str, str | None]:
        return "accepted", None

    async def raw(sock: Path, data: bytes) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(data)
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return reply

    async def test() -> None:
        sock = tmp_path / "console.sock"
        server = await console.serve(sock, handle)
        assert (await raw(sock, b"not json\n"))["outcome"] == "error"
        assert (await raw(sock, b'["a list"]\n'))["outcome"] == "error"
        assert (await raw(sock, b'{"name": 1, "text": "x"}\n'))["outcome"] == "error"
        assert (await raw(sock, b"x" * 10_000 + b"\n"))["outcome"] == "error"
        assert (await raw(sock, b'{"name": "a", "text": "!enter"}\n'))["outcome"] == "accepted"
        server.close()
        await server.wait_closed()

    run(test)


def test_config_from_the_environment() -> None:
    config = Config.from_env({"TARGET_HOST": "10.30.0.251", "BRAIN_PORT": "9000"})
    assert (config.target_host, config.vnc_port, config.port) == ("10.30.0.251", 5900, 9000)
    with pytest.raises(SystemExit, match="TARGET_HOST"):
        Config.from_env({})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_brain_stops_at_once_even_with_a_page_connected(tmp_path: Path) -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        port = free_port()
        config = Config(
            "127.0.0.1", server.port, "127.0.0.1", port, ":memory:", str(tmp_path / "console.sock")
        )
        stop = asyncio.Event()
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", server.port))
            task = asyncio.create_task(brain.serve(config, stop))
            async with asyncio.timeout(2):
                await brain.executor.online.wait()
            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws/overlay")
                await ws.receive()  # hello
                started = time.monotonic()
                stop.set()
                async with asyncio.timeout(1):
                    await task
                msg = await ws.receive()
                assert (msg.type, msg.data) == (WSMsgType.CLOSE, 1001)  # going away: the page shows offline
                assert time.monotonic() - started < 1
        await server.stop()

    run(test)


# --- step 4: chat adapters, admin requests ---------------------------------------------


def config_for(tmp_path: Path, **changes: Any) -> Config:
    fields: dict[str, Any] = {
        "target_host": "127.0.0.1",
        "vnc_port": 1,
        "bind": "127.0.0.1",
        "port": 0,
        "db": ":memory:",
        "console": str(tmp_path / "console.sock"),
        "youtube_key_file": str(tmp_path / "google-api-key"),
        "discord_token_file": str(tmp_path / "discord-bot-token"),
    }
    fields.update(changes)
    return Config(**fields)


def test_adapters_stay_off_without_their_secrets(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.start_adapters(config_for(tmp_path, discord_guild_id=111))
            assert brain.youtube is None and brain.discord is None
            assert brain.adapters()["youtube"]["state"] == "off"
            assert await brain.admin(["youtube", VIDEO]) == (
                "error",
                "no YouTube API key: see docs/phase4.md (step 4)",
            )

    run(test)


VIDEO = "dQw4w9WgXcQ"


def test_the_youtube_video_is_set_by_admin_and_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    followed: list[str | None] = []

    def follow(_self: YouTubeChat, video: str | None) -> None:  # instead of reaching YouTube
        followed.append(video)

    monkeypatch.setattr(YouTubeChat, "follow", follow)
    (tmp_path / "google-api-key").write_text("key\n")
    db = str(tmp_path / "brain.db")

    async def test() -> None:
        with Store(db) as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.start_adapters(config_for(tmp_path))
            assert brain.youtube is not None and brain.youtube.api_key == "key"
            assert followed == []  # nothing set yet
            ok, text = await brain.admin(["youtube", f"https://youtu.be/{VIDEO}"])
            assert ok == "ok" and VIDEO in text
            brain.youtube.quota.spend(5)  # a streamList call
            assert await brain.admin(["youtube", "not a video"]) == (
                "error",
                "that's not a YouTube video ID or URL",
            )
            await brain.stop_adapters()
        with Store(db) as store:  # brain restarts: it follows the same chat
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.start_adapters(config_for(tmp_path))
            assert followed == [VIDEO, VIDEO]
            assert brain.youtube is not None and brain.youtube.quota.spent() == 5  # the day's count goes on
            brain.youtube.counters.calls, brain.youtube.counters.stream_seconds = 3, 90.0
            assert "5 quota units today (3 calls since brain started, 30 s each)" in brain.status_text()
            assert (await brain.admin(["youtube", "off"]))[0] == "ok"
            assert followed[-1] is None and store.setting("youtube_video") is None
            await brain.stop_adapters()

    run(test)


def test_discord_starts_with_a_token_and_a_server_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(_self: discord.Client, _token: str, *, reconnect: bool = True) -> None:
        raise discord.LoginFailure("bad token")

    monkeypatch.setattr(discord.Client, "start", start)
    (tmp_path / "discord-bot-token").write_text("token\n")

    async def test() -> None:
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.start_adapters(config_for(tmp_path))  # no server ID: stays off
            assert brain.discord is None
            brain.start_adapters(config_for(tmp_path, discord_guild_id=111, discord_channel="#control"))
            assert brain.discord is not None and brain.discord.panel is not None
            async with asyncio.timeout(2):
                while brain.discord.state != "stopped":
                    await asyncio.sleep(0.01)
            status = brain.adapters()["discord"]
            assert (status["state"], status["channel"]) == ("stopped", "control")
            assert "Discord: stopped (Discord refused the bot token)" in brain.status_text()
            await brain.stop_adapters()

    run(test)


def test_chat_from_each_platform_is_marked_on_the_overlay() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            brain.handle(ChatMessage("yt:UCa", "Ann", "!grid", time.time(), frozenset({"moderator"})), "yt")
            brain.handle(ChatMessage("dc:42", "Dan", "!grid", time.time()), "dc")
            assert [(await feed.next("log"))["platform"] for _ in range(2)] == ["yt", "dc"]
            name, roles = brain.store.db.execute(
                "SELECT display_name, roles FROM users WHERE id = 'yt:UCa'"
            ).fetchone()
            assert (name, roles) == ("Ann", "moderator")

    run(test)


def test_admin_over_the_console_socket(tmp_path: Path) -> None:
    async def test() -> None:
        stop = asyncio.Event()
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            config = config_for(tmp_path, port=free_port())
            task = asyncio.create_task(brain.serve(config, stop))
            sock = Path(config.console)
            async with asyncio.timeout(2):
                while not sock.exists():
                    await asyncio.sleep(0.01)
            reply = await console.request(sock, {"admin": ["status"]})
            assert reply["outcome"] == "ok" and "Machine: not taking input" in str(reply["reason"])
            assert "YouTube: off (no API key)" in str(reply["reason"])
            assert (await console.request(sock, {"admin": ["fly"]}))["outcome"] == "error"
            assert (await console.request(sock, {"admin": "status"}))["outcome"] == "error"  # not a list
            stop.set()
            await task

    run(test)


def test_discord_settings_from_the_environment() -> None:
    config = Config.from_env({"TARGET_HOST": "x", "DISCORD_GUILD_ID": "123", "DISCORD_CHANNEL": ""})
    assert (config.discord_guild_id, config.discord_channel) == (123, "control")
    assert Config.from_env({"TARGET_HOST": "x"}).discord_guild_id is None
    with pytest.raises(SystemExit, match="numeric"):
        Config.from_env({"TARGET_HOST": "x", "DISCORD_GUILD_ID": "my server"})


def test_status_shows_the_last_error_while_not_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def follow(_self: YouTubeChat, _video: str | None) -> None:  # instead of reaching YouTube
        pass

    monkeypatch.setattr(YouTubeChat, "follow", follow)
    (tmp_path / "google-api-key").write_text("key")

    async def test() -> None:
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.start_adapters(config_for(tmp_path))
            assert brain.youtube is not None
            brain.youtube.state = "looking"
            brain.youtube.counters.errors.append("videos.list: ClientConnectorDNSError")
            assert (
                "YouTube: looking, 0 quota units today (last error: videos.list: ClientConnectorDNSError)"
                in brain.status_text()
            )
            brain.youtube.state = "streaming"
            assert "YouTube: streaming, 0 quota units today\n" in brain.status_text()

    run(test)


# --- Phase 5 step 2: the text filter in brain ------------------------------------------


def a_listed_word() -> str:
    """A word from the filter's lists (tests don't spell them out)."""
    for line in (LISTS / "en.txt").read_text("utf-8").splitlines():
        if line.startswith("*"):
            return line[1:]
    raise AssertionError("no * term in en.txt")


def test_names_holding_a_listed_word_are_anonymized_on_the_overlay() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            brain.handle(ChatMessage("yt:UCx", f"the {a_listed_word()} guy", "!grid", time.time()), "yt")
            line = await feed.next("log")
            assert line["user"].startswith("viewer#") and a_listed_word() not in json.dumps(line)

    run(test)


def test_a_word_typed_in_pieces_by_several_people_is_refused() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        word = a_listed_word()
        async with running(server) as (brain, feed):
            assert (await brain.console_message("ann", f"!type echo {word[:3]}"))[0] == "accepted"
            assert (await brain.console_message("ben", f"!type {word[3:]}")) == ("filtered", HATE)
            lines = [await feed.next("log") for _ in range(2)]
            assert lines[1]["user"] == "ben" and lines[1]["reason"] == HATE and word not in json.dumps(lines)
        await server.stop()

    run(test)


def test_three_refused_slurs_time_the_user_out_on_stream() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            word = a_listed_word()
            for _ in range(3):
                assert (await brain.console_message("troll", f"!type {word}"))[0] == "filtered"
            assert (await brain.console_message("troll", "!grid"))[0] == "banned"
            reasons: list[str | None] = []
            while not any("timed out" in str(r) for r in reasons):
                reasons.append((await feed.next("log"))["reason"])
            assert reasons[0] == HATE and reasons[-1] == "timed out for 10 minutes"
            assert brain.store.banned_until("console:troll") > time.time() + 590

    run(test)


# --- moderation: panic, bans, lockdown (Phase 5 step 3) -------------------------


class FakeIncus(Incus):
    """Snapshots succeed (or fail with `error`) and are recorded."""

    def __init__(self, error: Exception | None = None) -> None:  # no certificates needed
        self.taken: list[tuple[str, str, str]] = []
        self.error = error

    async def snapshot(self, name: str, *, instance: str = "", address: str = "") -> str:
        if self.error:
            raise self.error
        self.taken.append((name, instance, address))
        return instance or "found-by-address"


def quiet_discord() -> DiscordChat:
    """A Discord adapter that never connects: alerts wait in it."""
    return DiscordChat("token", 1, "control", lambda _msg: None, lambda: "")


def test_panic_covers_the_stream_freezes_input_and_resume_lifts_it() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        async with running(server) as (brain, feed):
            brain.discord = quiet_discord()
            answer = brain.moderation.panic("Mia (dc:1) on Discord", "nsfw")
            assert answer.ok
            assert await feed.next() == {"type": "panic", "on": True, "kind": "panic"}  # the first thing
            assert brain.executor.frozen
            assert await brain.console_message("alice", "!enter") == ("paused", PAUSED)
            await brain.console_message("bob", "!click A1")  # paused input stays off the overlay
            await brain.console_message("bob", "!grid")
            assert (await feed.next("log"))["text"] == "!grid"
            [snapshot] = brain.store.recent_events(("snapshot",))
            assert snapshot[3]["result"] == "off"
            alerts = brain.discord.pending_alerts()
            assert alerts[0].startswith("🚨 **Panic** by Mia (dc:1) on Discord: nsfw.")
            assert alerts[1].startswith("No forensic snapshot")
            assert "PANIC since" in brain.status_text()
            assert "Machine: input frozen by the panic" in brain.status_text()
            assert brain.moderation.resume("Mia").ok
            assert await feed.next("panic") == {"type": "panic", "on": False, "kind": "panic"}
            assert await brain.console_message("alice", "!enter") == ("accepted", None)
            assert (await feed.next("log-state"))["state"] == "done"
        await server.stop()

    run(test)


def test_a_page_connecting_during_a_panic_is_covered_at_once() -> None:
    async def test() -> None:
        async with running() as (brain, _feed):
            brain.moderation.panic("m")
            brain.moderation.lockdown("m", True)
            async with test_utils.TestClient(test_utils.TestServer(brain.web_app())) as client:
                ws = await client.ws_connect("/ws/overlay")
                hello = await Feed(ws).next("hello")
                assert hello["panic"] is True and hello["lockdown"] is True
                await ws.close()

    run(test)


def test_a_panic_outlives_a_restart() -> None:
    with Store(":memory:") as store:
        Brain(store, lambda: connect_to("127.0.0.1", 1)).moderation.panic("m")
        again = Brain(store, lambda: connect_to("127.0.0.1", 1))
        assert again.executor.frozen and again.overlay.panic_on
        assert again.moderation.resume("m").ok and not again.executor.frozen


def test_moderators_panic_from_chat_and_others_cant() -> None:
    async def test() -> None:
        async with running() as (brain, _feed):
            viewer = ChatMessage("yt:v", "Viewer", "!panic", time.time())
            assert brain.handle(viewer, "yt")[0] == "invalid"
            assert not brain.moderation.panicking
            mod = ChatMessage("yt:m", "Mod", "!PANIC spam on screen", time.time(), frozenset({"moderator"}))
            assert brain.handle(mod, "yt")[0] == "moderation"
            assert brain.moderation.panic_state is not None
            assert brain.moderation.panic_state["by"] == "Mod (yt:m) in YouTube chat"
            assert brain.moderation.panic_state["reason"] == "spam on screen"
            owner = ChatMessage("dc:o", "Owner", "!resume", time.time(), frozenset({"owner"}))
            assert brain.handle(owner, "dc") == (
                "moderation",
                "Resumed: the stream is live and chat has control again.",
            )
            again = ChatMessage("dc:o", "Owner", "!resume", time.time(), frozenset({"owner"}))
            assert brain.handle(again, "dc")[0] == "invalid"  # no panic to end
            normal = ChatMessage("dc:o", "Owner", "!enter", time.time(), frozenset({"owner"}))
            assert brain.handle(normal, "dc")[0] == "busy"  # other commands go through as usual

    run(test)


def test_forensic_snapshots_through_incus() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            incus = FakeIncus()
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1), incus, "10.30.0.251")
            brain.discord = quiet_discord()
            brain.moderation.panic("m", now=1_700_000_000.0)
            async with asyncio.timeout(2):
                while not store.recent_events(("snapshot",)):
                    await asyncio.sleep(0.01)
            assert incus.taken == [("panic-20231114-221320", "", "10.30.0.251")]
            [event] = store.recent_events(("snapshot",))
            assert event[3]["result"] == "ok" and event[3]["instance"] == "found-by-address"
            assert (
                "📸 Forensic snapshot `found-by-address/panic-20231114-221320` taken"
                in (brain.discord.pending_alerts()[-1])
            )
            brain.moderation.resume("m")
            brain.incus = FakeIncus(IncusError("not allowed"))
            brain.moderation.panic("m")
            async with asyncio.timeout(2):
                while len(store.recent_events(("snapshot",))) < 2:
                    await asyncio.sleep(0.01)
            assert store.recent_events(("snapshot",))[0][3]["error"] == "not allowed"
            assert brain.discord.pending_alerts()[-1].endswith("failed: not allowed")

    run(test)


def test_moderation_over_chat_brain_admin() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            brain.store.seen_user("yt:a", "Alice", time.time())
            assert await brain.admin(["ban", "Alice", "2h", "spamming", "links"]) == (
                "ok",
                "Alice (yt:a) banned for 2 hours: their commands are ignored on every platform.",
            )
            assert brain.store.recent_events(("ban",))[0][3]["reason"] == "spamming links"
            assert (await brain.admin(["ban", "yt:b", "perm"]))[0] == "ok"
            status, text = await brain.admin(["bans"])
            assert status == "ok" and "yt:b) forever" in text and "Alice (yt:a) until" in text
            assert await brain.admin(["timeout", "yt:c"]) == (
                "ok",
                "yt:c timed out for 10 minutes: their commands are ignored on every platform.",
            )
            assert (await brain.admin(["timeout", "yt:c", "30"]))[0] == "ok"
            assert (await brain.admin(["unban", "yt:a"]))[0] == "ok"
            assert (await brain.admin(["unban", "yt:a"]))[0] == "error"
            assert (await brain.admin(["lockdown", "on"]))[0] == "ok"
            assert await feed.next("lockdown") == {"type": "lockdown", "on": True}
            assert "Lockdown on since" in brain.status_text()
            assert (await brain.admin(["lockdown", "maybe"]))[0] == "error"
            assert (await brain.admin(["panic"]))[0] == "ok"
            assert (await brain.admin(["resume"]))[0] == "ok"
            assert brain.store.recent_events(("panic",))[0][3]["by"] == "console"
            status, usage = await brain.admin(["ban", "yt:x"])
            assert status == "error" and "ban WHO DURATION" in usage

    run(test)


def test_incus_settings(tmp_path: Path) -> None:
    config = Config.from_env({"TARGET_HOST": "x"})
    assert config.incus() == (None, "INCUS_URL isn't set")
    files = {name: tmp_path / name for name in ("c.crt", "c.key", "s.crt")}
    env = {
        "TARGET_HOST": "x",
        "INCUS_URL": "https://10.20.0.1:8443",
        "INCUS_CERT": str(files["c.crt"]),
        "INCUS_KEY": str(files["c.key"]),
        "INCUS_SERVER_CERT": str(files["s.crt"]),
    }
    incus, why = Config.from_env(env).incus()
    assert incus is None and why.startswith("missing ") and "c.key" in why
    for path in files.values():
        path.write_text("not a certificate")
    incus, why = Config.from_env(env).incus()
    assert incus is None and why.startswith("can't load the certificates")


# --- screen OCR (Phase 5 step 4) -------------------------------------------------


def ocr_hit(found: tuple[str, ...] = ("x badword",)) -> Hit:
    return Hit(1_700_000_000.0, found, found, Frame(2, 1, b"\x10\x20\x30\x00" * 2))


def test_an_ocr_hit_in_shadow_mode_is_only_reported(tmp_path: Path) -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            brain.discord = quiet_discord()
            brain.ocr_mode, brain.screens_dir = "shadow", tmp_path / "screens"
            brain.ocr_hit(ocr_hit())
            [event] = brain.store.recent_events(("ocr_hit",))
            assert event[3] == {
                "mode": "shadow",
                "found": ["x badword"],
                "screenshot": "ocr-20231114-221320.png",
            }
            assert (tmp_path / "screens" / "ocr-20231114-221320.png").read_bytes().startswith(b"\x89PNG")
            [alert] = brain.discord.pending_alerts()
            assert "shadow mode, nothing was done on stream" in alert and "||x badword||" in alert
            assert brain.discord._alerts[0][1] is not None  # pyright: ignore[reportPrivateUsage]  # the screenshot
            assert not brain.moderation.panicking and not brain.executor.frozen
            await brain.console_message("alice", "!grid")
            assert (await feed.next("log"))["text"] == "!grid"  # the feed carried no panic

    run(test)


def test_an_ocr_hit_in_enforce_mode_holds_until_a_moderator_resumes(tmp_path: Path) -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            brain.discord = quiet_discord()
            brain.ocr_mode, brain.screens_dir = "enforce", tmp_path
            cleared: list[str] = []
            brain.watch = cast(
                Any,
                SimpleNamespace(
                    clear=cleared.extend,
                    status=lambda: {"state": "reading", "reads": 0, "hits": 1, "last_error": ""},
                ),
            )
            brain.ocr_hit(ocr_hit(("x badword", "more")))
            assert await feed.next("panic") == {"type": "panic", "on": True, "kind": "hold"}
            assert brain.moderation.kind == "hold" and brain.executor.frozen
            assert "CONTENT HOLD since" in brain.status_text()
            assert brain.store.recent_events(("hold",))[0][3]["lines"] == ["x badword", "more"]
            assert brain.store.recent_events(("snapshot",))[0][3]["name"].startswith("hold-")
            alerts = brain.discord.pending_alerts()
            assert any("**Content hold** by screen OCR" in a for a in alerts)
            assert any("held the stream" in a for a in alerts)
            assert not brain.moderation.hold("screen OCR", ("again",)).ok  # one at a time
            assert brain.moderation.resume("Mia").ok
            assert await feed.next("panic") == {"type": "panic", "on": False, "kind": "hold"}
            assert cleared == ["x badword", "more"]  # what it saw is fine now
            assert not brain.executor.frozen

    run(test)


def test_a_panic_during_a_hold_becomes_a_panic() -> None:
    async def test() -> None:
        async with running() as (brain, feed):
            brain.moderation.hold("screen OCR", ("x",))
            await feed.next("panic")
            answer = brain.moderation.panic("Mia", "it's bad")
            assert answer.ok and "now a panic" in answer.text
            assert await feed.next("panic") == {"type": "panic", "on": True, "kind": "panic"}
            assert brain.moderation.kind == "panic"
            assert len(brain.store.recent_events(("snapshot",))) == 1  # the hold's snapshot is enough
            assert not brain.moderation.hold("screen OCR", ("y",)).ok  # already covered
            async with test_utils.TestClient(test_utils.TestServer(brain.web_app())) as client:
                ws = await client.ws_connect("/ws/overlay")
                hello = await Feed(ws).next("hello")
                assert hello["panic"] is True and hello["panic_kind"] == "panic"
                await ws.close()

    run(test)


def test_ocr_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = Config.from_env({"TARGET_HOST": "x"})
    assert (config.ocr_mode, config.ocr_interval, config.ocr_langs) == ("shadow", 5.0, "eng+ara")
    config = Config.from_env(
        {"TARGET_HOST": "x", "OCR_MODE": "enforce", "OCR_INTERVAL": "0.1", "OCR_LANGS": "eng+ara"}
    )
    assert (config.ocr_mode, config.ocr_interval, config.ocr_langs) == ("enforce", 1.0, "eng+ara")
    with pytest.raises(SystemExit, match="OCR_MODE"):
        Config.from_env({"TARGET_HOST": "x", "OCR_MODE": "loud"})
    with Store(":memory:") as store:
        brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
        brain.start_ocr(Config.from_env({"TARGET_HOST": "x", "OCR_MODE": "off"}))
        assert brain.watch is None and "Screen OCR: off" in brain.status_text()

        def which(found: str | None) -> Callable[[str], str | None]:
            return lambda _name: found

        monkeypatch.setattr(brain_module.shutil, "which", which(None))
        brain.start_ocr(Config.from_env({"TARGET_HOST": "x"}))
        assert brain.watch is None  # no tesseract
        monkeypatch.setattr(brain_module.shutil, "which", which("/usr/bin/tesseract"))
        brain.start_ocr(Config.from_env({"TARGET_HOST": "x", "SCREENS_DIR": str(tmp_path)}))
        assert brain.watch is not None and brain.ocr_mode == "shadow" and brain.screens_dir == tmp_path
        assert "Screen OCR: shadow, starting, 0 hits" in brain.status_text()


# --- retention and deletion requests (Phase 5 step 5) ------------------------------


def test_forget_over_chat_brain_admin() -> None:
    async def test() -> None:
        async with running() as (brain, _feed):
            await brain.console_message("alice", "!click Z1")
            await brain.console_message("bob", "!click Z2")
            status, text = await brain.admin(["forget", "alice"])
            assert status == "ok" and text.startswith("Forgot console:alice: 1 commands")
            assert [c["user"] for c in brain.store.recent_commands()] == ["console:bob"]
            assert (await brain.admin(["forget", "nobody-here"]))[0] == "error"

    run(test)


def test_the_retention_pass_deletes_old_logs_and_screenshots(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.screens_dir = tmp_path
            old, new = tmp_path / "ocr-20200101-000000.png", tmp_path / "ocr-20261116-000000.png"
            old.write_bytes(b"x")
            new.write_bytes(b"y")
            os.utime(old, (0, 0))
            store.seen_user("yt:a", "A", 0.0)
            store.record_command(0.0, "yt:a", "!enter", None, "accepted", None)
            task = asyncio.create_task(brain.keep_tidy())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not old.exists() and new.exists()
            assert store.recent_commands() == []

    run(test)
