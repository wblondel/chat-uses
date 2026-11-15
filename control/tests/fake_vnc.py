"""A fake VNC server for tests: well-behaved (records the input it gets) or
hostile (sends scripted bytes, whatever the client says)."""

import asyncio
import contextlib
import struct
from dataclasses import dataclass, field
from typing import Literal

VERSION = b"RFB 003.008\n"


def server_init(width: int = 1600, height: int = 900, name: bytes = b"fake") -> bytes:
    return struct.pack(">HH", width, height) + bytes(16) + struct.pack(">I", len(name)) + name


def handshake(width: int = 1600, height: int = 900, name: bytes = b"fake") -> bytes:
    """Everything a well-behaved server sends, in one go."""
    return VERSION + b"\x01\x01" + struct.pack(">I", 0) + server_init(width, height, name)


def reason(text: bytes) -> bytes:
    return struct.pack(">I", len(text)) + text


# Input events as the server saw them.
Key = tuple[Literal["key"], bool, int]  # ("key", down, keysym)
Pointer = tuple[Literal["ptr"], int, int, int]  # ("ptr", buttons, x, y)


@dataclass
class FakeVnc:
    width: int = 1600
    height: int = 900
    script: bytes | None = None  # hostile: send exactly this, then only listen
    after_init: bytes = b""  # sent after a good handshake
    events: list[Key | Pointer] = field(default_factory=lambda: [])
    client_replies: list[bytes] = field(default_factory=lambda: [])  # version, security, shared flag
    unknown: list[int] = field(default_factory=lambda: [])  # message types we didn't expect
    connections: int = 0
    hang_ups: int = 0  # connections finished, after reading all the client sent
    port: int = 0
    _server: asyncio.Server | None = None
    _writers: list[asyncio.StreamWriter] = field(default_factory=lambda: [])
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> "FakeVnc":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        """Stop listening and hang up on everyone."""
        if self._server:
            self._server.close()
        await self.hang_up()
        if self._server:
            await self._server.wait_closed()
            self._server = None

    async def hang_up(self) -> None:
        for writer in self._writers:
            writer.close()
        self._writers.clear()

    async def wait_for(self, count: int, timeout: float = 2.0) -> list[Key | Pointer]:
        """Wait until at least `count` input events have arrived."""
        async with asyncio.timeout(timeout):
            while len(self.events) < count:
                self._changed.clear()
                await self._changed.wait()
        return self.events

    async def wait_hung_up(self, count: int = 1, timeout: float = 2.0) -> list[Key | Pointer]:
        """Wait until `count` connections have ended, with all they sent read."""
        async with asyncio.timeout(timeout):
            while self.hang_ups < count:
                self._changed.clear()
                await self._changed.wait()
        return self.events

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            if self.script is not None:
                writer.write(self.script)
                await writer.drain()
                while await reader.read(4096):
                    pass
                return
            writer.write(VERSION)
            self.client_replies.append(await reader.readexactly(12))
            writer.write(b"\x01\x01")  # one security type: None
            self.client_replies.append(await reader.readexactly(1))
            writer.write(struct.pack(">I", 0))
            self.client_replies.append(await reader.readexactly(1))  # ClientInit
            writer.write(server_init(self.width, self.height) + self.after_init)
            await writer.drain()
            await self._read_input(reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.hang_ups += 1
            self._changed.set()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _read_input(self, reader: asyncio.StreamReader) -> None:
        while True:
            (kind,) = await reader.readexactly(1)
            if kind == 4:
                down, keysym = struct.unpack(">BxxI", await reader.readexactly(7))
                self.events.append(("key", bool(down), keysym))
            elif kind == 5:
                buttons, x, y = struct.unpack(">BHH", await reader.readexactly(5))
                self.events.append(("ptr", buttons, x, y))
            else:
                self.unknown.append(kind)
                return
            self._changed.set()
