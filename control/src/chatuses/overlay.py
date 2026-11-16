"""The overlay's live feed: brain → layout page, over a websocket (PLAN.md 5.6).

One way only. The page (in bcast's kiosk browser) gets JSON messages and
sends nothing that brain reads. Everything in them is already public (it is
what the stream shows), so the feed needs no password. Messages:

    {"type": "hello", "log": [line...], "target": bool,     on connect
     "panic": bool, "lockdown": bool}
    {"type": "log", ...line}                                a command, as chat sees it
    {"type": "log-state", "id": int, "state": str}          what became of it
    {"type": "pointer", "x": int, "y": int, "user": str}    who moved the pointer
    {"type": "grid"}                                        show the grid lines (!grid)
    {"type": "status", "target": bool}                      the target's input is up or down
    {"type": "panic", "on": bool}                           cover the stream at once (PLAN.md 7.5)
    {"type": "lockdown", "on": bool}                        only regulars may send commands

bcast follows "panic" too (chat-bcast-panic), to mute the target's sound.

A line is {"id", "user", "platform", "text", "state", "reason"}; `text` is
built from the parsed command (commands.describe), never copied from chat,
and refusals carry only brain's fixed wording (D32).

Each page gets its own bounded queue: a page that stops reading is
disconnected (it reconnects and gets a fresh hello), so it can't slow brain
down or make it buffer without limit.
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

log = logging.getLogger(__name__)

MAX_CLIENTS = 16
CLIENT_QUEUE = 256  # messages waiting for one page before it's dropped
KEEP_LINES = 12  # log lines sent to a page that connects


@dataclass
class Line:
    id: int
    user: str
    platform: str
    text: str | None  # None for refusals: they show only the reason
    state: str  # queued, done, dropped, refused, later
    reason: str | None = None


class OverlayHub:
    def __init__(self) -> None:
        self.target_online = False
        self.panic_on = False
        self.lockdown_on = False
        self._clients: set[asyncio.Queue[str | None]] = set()
        self._sockets: set[web.WebSocketResponse] = set()
        self._lines: deque[Line] = deque(maxlen=KEEP_LINES)
        self._next_id = 1

    @property
    def clients(self) -> int:
        return len(self._clients)

    # --- what brain reports --------------------------------------------------

    def add_line(
        self, user: str, platform: str, text: str | None, state: str, reason: str | None = None
    ) -> int:
        line = Line(self._next_id, user, platform, text, state, reason)
        self._next_id += 1
        self._lines.append(line)
        self._send({"type": "log", **asdict(line)})
        return line.id

    def set_state(self, line_id: int, state: str) -> None:
        for line in self._lines:
            if line.id == line_id:
                line.state = state
        self._send({"type": "log-state", "id": line_id, "state": state})

    def pointer(self, x: int, y: int, user: str) -> None:
        self._send({"type": "pointer", "x": x, "y": y, "user": user})

    def grid(self) -> None:
        self._send({"type": "grid"})

    def status(self, target_online: bool) -> None:
        self.target_online = target_online
        self._send({"type": "status", "target": target_online})

    def panic(self, on: bool) -> None:
        self.panic_on = on
        self._send({"type": "panic", "on": on})

    def lockdown(self, on: bool) -> None:
        self.lockdown_on = on
        self._send({"type": "lockdown", "on": on})

    async def close_all(self) -> None:
        """Let every page go (brain is stopping): they show "Controls offline" at once."""
        await asyncio.gather(
            *(ws.close(code=WSCloseCode.GOING_AWAY, message=b"brain stopping") for ws in list(self._sockets)),
            return_exceptions=True,
        )

    def _send(self, message: dict[str, Any]) -> None:
        text = json.dumps(message, ensure_ascii=False)
        for queue in list(self._clients):
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                log.warning("an overlay page stopped reading: disconnecting it")
                self._clients.discard(queue)
                queue.get_nowait()  # make room for the end marker
                queue.put_nowait(None)

    # --- the websocket ---------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if len(self._clients) >= MAX_CLIENTS:
            raise web.HTTPServiceUnavailable(text="too many overlay pages\n")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
        await ws.prepare(request)
        queue: asyncio.Queue[str | None] = asyncio.Queue(CLIENT_QUEUE)
        hello = {
            "type": "hello",
            "log": [asdict(line) for line in self._lines],
            "target": self.target_online,
            "panic": self.panic_on,
            "lockdown": self.lockdown_on,
        }
        queue.put_nowait(json.dumps(hello, ensure_ascii=False))
        self._clients.add(queue)
        self._sockets.add(ws)
        sender = asyncio.create_task(self._sender(ws, queue))
        try:
            async for msg in ws:  # nothing the page sends is used; this notices it leaving
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(queue)
            self._sockets.discard(ws)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            await ws.close()
        return ws

    async def _sender(self, ws: web.WebSocketResponse, queue: asyncio.Queue[str | None]) -> None:
        while (text := await queue.get()) is not None:
            await ws.send_str(text)
        await ws.close()
