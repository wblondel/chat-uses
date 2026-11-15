import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError, test_utils, web

from chatuses import overlay
from chatuses.overlay import OverlayHub


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def app_for(hub: OverlayHub) -> web.Application:
    app = web.Application()
    app.router.add_get("/ws/overlay", hub.handle)
    return app


async def receive(ws: Any) -> dict[str, Any]:
    msg = await asyncio.wait_for(ws.receive(), 2)
    assert msg.type == WSMsgType.TEXT, msg
    return json.loads(msg.data)


def test_a_page_gets_the_recent_log_then_live_updates() -> None:
    async def test() -> None:
        hub = OverlayHub()
        first = hub.add_line("alice", "yt", "!type hello", "queued")
        hub.status(True)
        async with test_utils.TestClient(test_utils.TestServer(app_for(hub))) as client:
            ws = await client.ws_connect("/ws/overlay")
            hello = await receive(ws)
            assert hello["type"] == "hello" and hello["target"] is True
            assert [line["text"] for line in hello["log"]] == ["!type hello"]
            assert hub.clients == 1

            hub.set_state(first, "done")
            hub.add_line("bob", "dc", None, "refused", "slow down")
            hub.pointer(550, 650, "alice")
            hub.grid()
            assert await receive(ws) == {"type": "log-state", "id": first, "state": "done"}
            line = await receive(ws)
            assert (line["type"], line["user"], line["text"], line["reason"]) == (
                "log",
                "bob",
                None,
                "slow down",
            )
            assert await receive(ws) == {"type": "pointer", "x": 550, "y": 650, "user": "alice"}
            assert await receive(ws) == {"type": "grid"}

            await ws.send_str("anything: the page has no say")
            hub.status(False)
            assert await receive(ws) == {"type": "status", "target": False}
            await ws.close()
        assert hub.clients == 0

    run(test)


def test_a_reconnecting_page_sees_line_states_as_they_are_now() -> None:
    async def test() -> None:
        hub = OverlayHub()
        line = hub.add_line("alice", "yt", "!enter", "queued")
        hub.set_state(line, "dropped")
        async with test_utils.TestClient(test_utils.TestServer(app_for(hub))) as client:
            ws = await client.ws_connect("/ws/overlay")
            assert (await receive(ws))["log"][0]["state"] == "dropped"
            await ws.close()

    run(test)


def test_only_the_last_lines_are_kept() -> None:
    hub = OverlayHub()
    for i in range(overlay.KEEP_LINES + 5):
        hub.add_line("alice", "yt", f"!type {i}", "queued")

    async def test() -> None:
        async with test_utils.TestClient(test_utils.TestServer(app_for(hub))) as client:
            ws = await client.ws_connect("/ws/overlay")
            lines = (await receive(ws))["log"]
            assert len(lines) == overlay.KEEP_LINES and lines[-1]["text"] == f"!type {overlay.KEEP_LINES + 4}"
            await ws.close()

    run(test)


def test_a_page_that_falls_behind_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(overlay, "CLIENT_QUEUE", 3)

    async def test() -> None:
        hub = OverlayHub()
        async with test_utils.TestClient(test_utils.TestServer(app_for(hub))) as client:
            ws = await client.ws_connect("/ws/overlay")
            await receive(ws)  # hello
            for i in range(10):  # no await in between: the page can't keep up
                hub.add_line("spam", "yt", f"!type {i}", "queued")
            assert hub.clients == 0
            kinds: list[WSMsgType] = []
            while (msg := await asyncio.wait_for(ws.receive(), 2)).type == WSMsgType.TEXT:
                kinds.append(msg.type)
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
            assert len(kinds) <= 3  # what fitted in its queue, then it was let go
            hub.add_line("next", "yt", "!enter", "queued")  # the hub carries on
            ws2 = await client.ws_connect("/ws/overlay")
            assert (await receive(ws2))["log"][-1]["user"] == "next"
            await ws2.close()

    run(test)


def test_too_many_pages_are_turned_away(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(overlay, "MAX_CLIENTS", 1)

    async def test() -> None:
        hub = OverlayHub()
        async with test_utils.TestClient(test_utils.TestServer(app_for(hub))) as client:
            ws = await client.ws_connect("/ws/overlay")
            await receive(ws)
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws/overlay")
            await ws.close()

    run(test)


def test_a_huge_message_from_a_page_closes_it() -> None:
    async def test() -> None:
        hub = OverlayHub()
        async with test_utils.TestClient(test_utils.TestServer(app_for(hub))) as client:
            ws = await client.ws_connect("/ws/overlay")
            await receive(ws)
            await ws.send_str("x" * 100_000)
            msg = await asyncio.wait_for(ws.receive(), 2)
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
            await ws.close()
        assert hub.clients == 0

    run(test)
