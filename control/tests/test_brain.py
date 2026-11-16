import asyncio
import json
import socket
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import discord
import pytest
from aiohttp import WSMsgType, test_utils
from fake_vnc import FakeVnc

from chatuses import brain as brain_module
from chatuses import console, executor
from chatuses.adapters.youtube import YouTubeChat
from chatuses.brain import Brain, Config
from chatuses.executor import connect_to
from chatuses.pipeline import ChatMessage
from chatuses.safety.textfilter import HATE, LISTS
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
async def running(server: FakeVnc | None = None) -> AsyncGenerator[tuple[Brain, Feed]]:
    """A brain with its executor running, and an overlay page connected to it."""
    port = server.port if server else 1  # port 1: nothing listens, the target stays offline
    with Store(":memory:") as store:
        brain = Brain(store, lambda: connect_to("127.0.0.1", port))
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
            await brain.console_message("alice", "!democracy")
            line = await feed.next("log")
            assert (line["text"], line["state"]) == ("!democracy", "later")

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
            task = asyncio.create_task(brain.run(config, stop))
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
            task = asyncio.create_task(brain.run(config, stop))
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
            assert await brain.admin(["youtube", "not a video"]) == (
                "error",
                "that's not a YouTube video ID or URL",
            )
            await brain.stop_adapters()
        with Store(db) as store:  # brain restarts: it follows the same chat
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            brain.start_adapters(config_for(tmp_path))
            assert followed == [VIDEO, VIDEO]
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
            assert brain.discord is not None
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
            task = asyncio.create_task(brain.run(config, stop))
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
