import asyncio
import json
import socket
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType, test_utils
from fake_vnc import FakeVnc

from chatuses import brain as brain_module
from chatuses import console, executor
from chatuses.brain import Brain, Config
from chatuses.executor import connect_to
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
