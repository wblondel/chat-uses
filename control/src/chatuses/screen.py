"""The target's screen, read over VNC (PLAN.md 5.4, 7.4): frames for screen
OCR now, for the watchdog and thumbnails later.

A second connection, apart from the input one (vnc.py), that only reads.
The server runs where chat is root, so it is hostile:

- only the Raw encoding is asked for, and nothing else is accepted;
- every rectangle must lie inside the screen, and the screen is capped at
  MAX_PIXELS, so a frame can't take more than ~33 MB;
- a whole frame must arrive within FRAME_TIMEOUT;
- the other messages a server may send (colour map, bell, clipboard) are
  read within caps and thrown away.

Pixels come as 32-bit true colour, red, green, blue in bytes 0, 1, 2 of
each (the format asked for), so the RGB picture is three byte slices.
"""

import asyncio
import contextlib
import hashlib
import struct
import zlib
from dataclasses import dataclass

from chatuses.vnc import TIMEOUT, VncError, handshake

FRAME_TIMEOUT = 15.0  # seconds for a whole frame (1600x900 raw is 5.8 MB)
MAX_PIXELS = 3840 * 2160
MAX_CUT_TEXT = 1 << 20  # bytes of clipboard a server may send (read and dropped)
RAW = 0
# SetPixelFormat: 32 bpp, depth 24, little-endian, true colour, 8 bits per
# channel, red shift 0, green 8, blue 16: bytes R, G, B, unused.
PIXEL_FORMAT = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    rgbx: bytes  # 4 bytes a pixel: red, green, blue, unused

    def rgb(self) -> bytes:
        out = bytearray(self.width * self.height * 3)
        for channel in range(3):
            out[channel::3] = self.rgbx[channel::4]
        return bytes(out)

    def digest(self) -> str:
        return hashlib.blake2b(self.rgbx, digest_size=16).hexdigest()

    def ppm(self) -> bytes:
        """Netpbm P6, what tesseract reads from stdin without any library."""
        return f"P6\n{self.width} {self.height}\n255\n".encode() + self.rgb()

    def png(self) -> bytes:
        """PNG (RGB, 8-bit), for alerts and the dashboard: standard library only."""
        rgb, stride = self.rgb(), self.width * 3
        rows = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(self.height))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b"")
        )


class ScreenReader:
    def __init__(self, host: str, port: int = 5900, timeout: float = TIMEOUT) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.width = self.height = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pixels = bytearray()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
            self.width, self.height, _name = await asyncio.wait_for(
                handshake(self._reader, self._writer), self.timeout
            )
            if self.width * self.height > MAX_PIXELS:
                raise VncError(f"screen too big ({self.width}x{self.height})")
            self._writer.write(b"\x00\x00\x00\x00" + PIXEL_FORMAT)  # SetPixelFormat
            self._writer.write(struct.pack(">BxHi", 2, 1, RAW))  # SetEncodings: Raw only
            await asyncio.wait_for(self._writer.drain(), self.timeout)
            self._pixels = bytearray(self.width * self.height * 4)
        except (OSError, asyncio.IncompleteReadError, TimeoutError, VncError) as err:
            await self.close()
            if isinstance(err, VncError):
                raise
            raise VncError(
                f"cannot read the screen at {self.host}:{self.port}: {err or type(err).__name__}"
            ) from err
        except asyncio.CancelledError:
            await self.close()
            raise

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self._writer.wait_closed(), self.timeout)
        self._reader = self._writer = None

    async def grab(self) -> Frame:
        """The whole screen, now. Any trouble closes the connection (VncError)."""
        if not self._writer or not self._reader:
            raise VncError("not connected")
        try:
            self._writer.write(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
            await asyncio.wait_for(self._read_update(self._reader), FRAME_TIMEOUT)
        except (OSError, asyncio.IncompleteReadError, TimeoutError, VncError) as err:
            await self.close()
            if isinstance(err, VncError):
                raise
            raise VncError(f"screen read failed: {err or type(err).__name__}") from err
        return Frame(self.width, self.height, bytes(self._pixels))

    async def _read_update(self, reader: asyncio.StreamReader) -> None:
        """Read messages until a framebuffer update, and apply it."""
        while True:
            (kind,) = await reader.readexactly(1)
            if kind == 0:  # FramebufferUpdate
                (count,) = struct.unpack(">xH", await reader.readexactly(3))
                for _ in range(count):
                    await self._read_rect(reader)
                return
            if kind == 1:  # SetColourMapEntries: not used in true colour
                _first, entries = struct.unpack(">xHH", await reader.readexactly(5))
                await reader.readexactly(entries * 6)
            elif kind == 2:  # Bell
                pass
            elif kind == 3:  # ServerCutText: read and dropped
                (length,) = struct.unpack(">xxxI", await reader.readexactly(7))
                if length > MAX_CUT_TEXT:
                    raise VncError("clipboard text too long")
                await reader.readexactly(length)
            else:
                raise VncError(f"unexpected message type {kind}")

    async def _read_rect(self, reader: asyncio.StreamReader) -> None:
        x, y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
        if encoding != RAW:
            raise VncError(f"unexpected encoding {encoding}")
        if x + w > self.width or y + h > self.height:
            raise VncError(f"rectangle {w}x{h}+{x}+{y} outside the {self.width}x{self.height} screen")
        stride, row = self.width * 4, w * 4
        for line in range(h):
            data = await reader.readexactly(row)
            start = (y + line) * stride + x * 4
            self._pixels[start : start + row] = data
