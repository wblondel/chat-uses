"""VNC (RFB 3.8, RFC 6143) input client: keys and pointer, nothing else.

The VNC server runs inside the target, where chat is root, so it is treated
as hostile (PLAN.md 5.4): every step has a timeout, every length the server
sends is capped, and after the handshake nothing it sends is parsed, only
read and thrown away (which also notices a disconnect). The client never
asks for screen updates. It joins as a shared client, so bcast's view stays
connected.

Security type "None" only: VNC is reachable only from brain, bcast and the
admin tunnel (PLAN.md 5.3).
"""

import asyncio
import contextlib
import struct

from chatuses.keymap import KeyCombo

TIMEOUT = 5.0  # seconds for connecting, each handshake step and each send
MAX_NAME = 1024  # bytes of desktop name accepted
MAX_REASON = 1024  # bytes of refusal reason accepted

BUTTONS = {"left": 1, "middle": 2, "right": 4}
WHEEL = {"up": 8, "down": 16}

DOUBLE_CLICK_GAP = 0.06  # seconds; well inside any double-click time
DRAG_STEP = 0.015  # seconds between pointer moves while dragging
SCROLL_GAP = 0.03  # seconds between wheel notches


class VncError(Exception):
    """The server broke the protocol, refused us, or went away."""


class VncInput:
    def __init__(self, host: str, port: int = 5900, timeout: float = TIMEOUT) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.width = self.height = 0
        self.name = ""
        self.lost = asyncio.Event()  # set once the connection is gone, for whatever reason
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._x = self._y = 0
        self._buttons = 0
        self._keys_down: set[int] = set()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing() and not self.lost.is_set()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
            await asyncio.wait_for(self._handshake(), self.timeout)
        except (TimeoutError, OSError, asyncio.IncompleteReadError, VncError) as err:
            await self.close()
            raise VncError(f"cannot connect to {self.host}:{self.port}: {err or type(err).__name__}") from err
        except asyncio.CancelledError:  # shutting down mid-handshake: don't leave the socket open
            await self.close()
            raise
        self._drain_task = asyncio.create_task(self._drain())

    async def close(self) -> None:
        """Let go of anything still held down, then hang up."""
        if self.connected and (self._keys_down or self._buttons):
            with contextlib.suppress(VncError, TimeoutError):
                await asyncio.wait_for(self._release(), self.timeout)
        self.lost.set()
        if self._drain_task:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None
        if self._writer:
            self._writer.close()
            with contextlib.suppress(OSError):
                await asyncio.wait_for(self._writer.wait_closed(), self.timeout)
        self._reader = self._writer = None

    async def _handshake(self) -> None:
        reader, writer = self._reader, self._writer
        assert reader and writer
        version = await reader.readexactly(12)
        if version != b"RFB 003.008\n":
            raise VncError("server doesn't speak RFB 3.8")
        writer.write(b"RFB 003.008\n")
        (count,) = await reader.readexactly(1)
        if count == 0:
            raise VncError(f"server refused: {await self._reason()}")
        types = await reader.readexactly(count)
        if 1 not in types:
            raise VncError("server wants authentication (security type None not offered)")
        writer.write(b"\x01")
        (result,) = struct.unpack(">I", await reader.readexactly(4))
        if result != 0:
            raise VncError(f"server refused: {await self._reason()}")
        writer.write(b"\x01")  # ClientInit: shared
        self.width, self.height = struct.unpack(">HH", await reader.readexactly(4))
        await reader.readexactly(16)  # pixel format: unused, nothing is drawn
        (name_len,) = struct.unpack(">I", await reader.readexactly(4))
        if name_len > MAX_NAME:
            raise VncError("desktop name too long")
        self.name = (await reader.readexactly(name_len)).decode("utf-8", "replace")
        if not (self.width and self.height):
            raise VncError("server reports an empty screen")

    async def _reason(self) -> str:
        assert self._reader
        (length,) = struct.unpack(">I", await self._reader.readexactly(4))
        if length > MAX_REASON:
            return "(reason too long)"
        return (await self._reader.readexactly(length)).decode("utf-8", "replace")[:200]

    async def _release(self) -> None:
        for keysym in list(self._keys_down):
            await self.key(keysym, False)
        if self._buttons:
            await self.pointer(self._x, self._y, 0)

    async def _drain(self) -> None:
        """Read and discard what the server sends, until it hangs up."""
        reader, writer = self._reader, self._writer
        assert reader and writer
        try:
            with contextlib.suppress(OSError):
                while await reader.read(65536):
                    pass
        finally:
            self.lost.set()
            writer.close()

    async def _send(self, data: bytes) -> None:
        if not self.connected:
            raise VncError("not connected")
        assert self._writer
        self._writer.write(data)
        try:
            await asyncio.wait_for(self._writer.drain(), self.timeout)
        except (TimeoutError, OSError) as err:
            self.lost.set()
            self._writer.close()
            raise VncError(f"connection lost: {err or type(err).__name__}") from err

    # --- primitives -------------------------------------------------------

    async def key(self, keysym: int, down: bool) -> None:
        await self._send(struct.pack(">BBxxI", 4, int(down), keysym))
        if down:
            self._keys_down.add(keysym)
        else:
            self._keys_down.discard(keysym)

    async def pointer(self, x: int, y: int, buttons: int | None = None) -> None:
        self._x = max(0, min(x, self.width - 1))
        self._y = max(0, min(y, self.height - 1))
        if buttons is not None:
            self._buttons = buttons
        await self._send(struct.pack(">BBHH", 5, self._buttons, self._x, self._y))

    # --- what commands are made of -------------------------------------------

    async def combo(self, combo: KeyCombo) -> None:
        for modifier in combo.modifiers:
            await self.key(modifier, True)
        await self.key(combo.key, True)
        await self.key(combo.key, False)
        for modifier in reversed(combo.modifiers):
            await self.key(modifier, False)

    async def click(self, x: int, y: int, button: int, count: int = 1) -> None:
        await self.pointer(x, y, 0)
        for i in range(count):
            if i:
                await asyncio.sleep(DOUBLE_CLICK_GAP)
            await self.pointer(x, y, button)
            await self.pointer(x, y, 0)

    async def drag(self, start: tuple[int, int], end: tuple[int, int], steps: int = 10) -> None:
        await self.pointer(*start, 0)
        await self.pointer(*start, BUTTONS["left"])
        for i in range(1, steps + 1):
            await asyncio.sleep(DRAG_STEP)
            await self.pointer(
                start[0] + (end[0] - start[0]) * i // steps, start[1] + (end[1] - start[1]) * i // steps
            )
        await self.pointer(*end, 0)

    async def scroll(self, direction: str, notches: int) -> None:
        for i in range(notches):
            if i:
                await asyncio.sleep(SCROLL_GAP)
            await self.pointer(self._x, self._y, WHEEL[direction])
            await self.pointer(self._x, self._y, 0)
