"""The overlay's live feed: brain → layout page, over a websocket (PLAN.md 5.6).

One way only. The page (in bcast's kiosk browser) gets JSON messages and
sends nothing that brain reads. Everything in them is already public (it is
what the stream shows), so the feed needs no password. Messages:

    {"type": "hello", "log": [line...], "target": bool,     on connect
     "panic": bool, "panic_kind": str, "lockdown": bool,
     "vote": {...} | null, "timeline": {...} | null, "mode": {...} | null,
     "run": {...} | null, "card": {...} | null}
    {"type": "log", ...line}                                a command, as chat sees it
    {"type": "log-state", "id": int, "state": str}          what became of it
    {"type": "pointer", "x": int, "y": int, "user": str}    who moved the pointer
    {"type": "grid"}                                        show the grid lines (!grid)
    {"type": "status", "target": bool}                      the target's input is up or down
    {"type": "panic", "on": bool, "kind": str}              cover the stream at once (PLAN.md 7.5);
                                                            kind "hold": a content hold
    {"type": "lockdown", "on": bool}                        only regulars may send commands
    {"type": "vote", "vote": {...} | null}                  the power vote, its count and time left
                                                            (referee/votes.py view()); null: none
    {"type": "mode", "mode": ..., "meter": int, ...}         anarchy or democracy, the meter, democracy's
                                                            tally (referee/modes.py view())
    {"type": "timeline", "timeline": [...], "saves": [...],  chat's snapshots (snapshots.py view()):
     "slots": int}                                          thumbnails at /thumbs/<id>.png
    {"type": "run", "run": {...} | null}                     the run: number, lives, uptime, a death
                                                            vote (game/runs.py view())
    {"type": "card", "kind": "death" | "start",             a full-frame card for `seconds`: a dead
     "seconds": float, ...}                                 run's (brain's end_run), a new run's
    {"type": "achievement", "name", "description",          unlocked (game/achievements.py): a toast
     "rewards": [str]}

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
import time
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
        self.panic_kind = "panic"
        self.lockdown_on = False
        self._vote: dict[str, Any] | None = None
        self._vote_at = 0.0  # when it was last sent (monotonic): a new page gets the time left
        self._timeline: dict[str, Any] | None = None
        self._mode: dict[str, Any] | None = None
        self._mode_at = 0.0
        self._run: dict[str, Any] | None = None
        self._run_at = 0.0
        self._driver: dict[str, Any] | None = None  # the driver's seat (game/driver.py)
        self._driver_at = 0.0
        self._card: dict[str, Any] | None = None
        self._card_until = 0.0
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

    def panic(self, on: bool, kind: str = "panic") -> None:
        self.panic_on, self.panic_kind = on, kind
        self._send({"type": "panic", "on": on, "kind": kind})

    def lockdown(self, on: bool) -> None:
        self.lockdown_on = on
        self._send({"type": "lockdown", "on": on})

    def vote(self, view: dict[str, Any] | None) -> None:
        self._vote, self._vote_at = view, time.monotonic()
        self._send({"type": "vote", "vote": view})

    def mode(self, view: dict[str, Any]) -> None:
        self._mode, self._mode_at = view, time.monotonic()
        self._send({"type": "mode", **view})

    def _mode_now(self) -> dict[str, Any] | None:
        if self._mode is None:
            return None
        gone = time.monotonic() - self._mode_at
        view = {**self._mode, "locked_for": max(0, round(self._mode.get("locked_for", 0) - gone))}
        tally = self._mode.get("tally")
        if tally and tally.get("ends_in") is not None:
            view["tally"] = {**tally, "ends_in": round(max(0.0, tally["ends_in"] - gone), 1)}
        return view

    def timeline(self, view: dict[str, Any]) -> None:
        self._timeline = view
        self._send({"type": "timeline", **view})

    def run(self, view: dict[str, Any] | None) -> None:
        self._run, self._run_at = view, time.monotonic()
        self._send({"type": "run", "run": view})

    def _run_now(self) -> dict[str, Any] | None:
        if self._run is None:
            return None
        gone = time.monotonic() - self._run_at
        view = {**self._run, "uptime": round(self._run["uptime"] + gone)}
        death = view.get("death")
        if death and death.get("ends_in") is not None:
            view["death"] = {**death, "ends_in": round(max(0.0, death["ends_in"] - gone), 1)}
        return view

    def driver(self, view: dict[str, Any] | None) -> None:
        """The driver's seat: {"name", "state": offered | driving, "ends_in"}, or None."""
        self._driver, self._driver_at = view, time.monotonic()
        self._send({"type": "driver", "driver": view})

    def _driver_now(self) -> dict[str, Any] | None:
        if self._driver is None:
            return None
        gone = time.monotonic() - self._driver_at
        return {**self._driver, "ends_in": round(max(0.0, self._driver["ends_in"] - gone), 1)}

    def achievement(self, data: dict[str, Any]) -> None:
        self._send({"type": "achievement", **data})

    def card(self, kind: str, data: dict[str, Any], seconds: float) -> None:
        self._card = {"type": "card", "kind": kind, **data, "seconds": seconds}
        self._card_until = time.monotonic() + seconds
        self._send(self._card)

    def _card_now(self) -> dict[str, Any] | None:
        left = self._card_until - time.monotonic()
        return {**self._card, "seconds": round(left, 1)} if self._card is not None and left > 1 else None

    def _vote_now(self) -> dict[str, Any] | None:
        if self._vote is None:
            return None
        left = max(0.0, float(self._vote.get("ends_in", 0)) - (time.monotonic() - self._vote_at))
        return {**self._vote, "ends_in": round(left, 1)}

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
            "panic_kind": self.panic_kind,
            "lockdown": self.lockdown_on,
            "vote": self._vote_now(),
            "timeline": self._timeline,
            "mode": self._mode_now(),
            "run": self._run_now(),
            "card": self._card_now(),
            "driver": self._driver_now(),
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
