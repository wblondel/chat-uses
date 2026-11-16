import asyncio
import struct
import zlib
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from fake_vnc import FakeVnc

from chatuses import screen as screen_module
from chatuses.screen import PIXEL_FORMAT, Frame, ScreenReader
from chatuses.vnc import VncError


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


async def reader_for(server: FakeVnc) -> ScreenReader:
    reader = ScreenReader("127.0.0.1", server.port)
    await reader.connect()
    return reader


def rect(x: int, y: int, w: int, h: int, encoding: int = 0) -> bytes:
    return struct.pack(">HHHHi", x, y, w, h, encoding)


def update(*rects: bytes) -> bytes:
    return b"\x00\x00" + struct.pack(">H", len(rects)) + b"".join(rects)


def test_a_whole_screen_in_the_format_asked_for() -> None:
    async def test() -> None:
        server = await FakeVnc(width=4, height=2).start()
        reader = await reader_for(server)
        frame = await reader.grab()
        assert (frame.width, frame.height) == (4, 2)
        assert frame.rgbx == b"\x10\x20\x30\x00" * 8
        assert frame.rgb() == b"\x10\x20\x30" * 8
        assert server.pixel_format == PIXEL_FORMAT  # 32 bpp, R G B in bytes 0 1 2
        assert server.encodings == [0]  # Raw only
        assert server.requests == [(0, 0, 0, 4, 2)]  # the whole screen, not incremental
        await reader.close()
        await server.stop()

    run(test)


def test_partial_rectangles_and_other_messages_are_handled() -> None:
    async def test() -> None:
        noise = b"\x02" + b"\x03\x00\x00\x00" + struct.pack(">I", 5) + b"hello"  # bell, clipboard
        noise += b"\x01\x00" + struct.pack(">HH", 0, 2) + bytes(12)  # colour map entries
        pixel = b"\xff\x00\x00\x00"
        server = await FakeVnc(width=3, height=2, update=noise + update(rect(1, 1, 2, 1) + pixel * 2)).start()
        reader = await reader_for(server)
        frame = await reader.grab()
        assert frame.rgbx == bytes(4) * 4 + pixel * 2  # the rest stays black
        await reader.close()
        await server.stop()

    run(test)


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (update(rect(0, 0, 2, 2, encoding=7) + bytes(16)), "unexpected encoding 7"),
        (update(rect(3, 0, 2, 1) + bytes(8)), "outside the 4x2 screen"),
        (update(rect(0, 0, 65535, 65535)), "outside"),
        (b"\x07", "unexpected message type 7"),
        (b"\x03\x00\x00\x00" + struct.pack(">I", 1 << 30), "clipboard text too long"),
        (update(rect(0, 0, 4, 2)) + bytes(5), "screen read failed"),  # cut short, then silence
    ],
)
def test_a_hostile_server_is_refused(bad: bytes, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen_module, "FRAME_TIMEOUT", 0.3)

    async def test() -> None:
        server = await FakeVnc(width=4, height=2, update=bad).start()
        reader = await reader_for(server)
        with pytest.raises(VncError, match=message):
            await reader.grab()
        assert not reader.connected  # closed: the next read starts over
        with pytest.raises(VncError, match="not connected"):
            await reader.grab()
        await server.stop()

    run(test)


def test_a_screen_too_big_is_refused() -> None:
    async def test() -> None:
        server = await FakeVnc(width=65535, height=65535).start()
        with pytest.raises(VncError, match="screen too big"):
            await reader_for(server)
        await server.stop()

    run(test)


def test_no_server_is_a_vnc_error() -> None:
    async def test() -> None:
        with pytest.raises(VncError, match="cannot read the screen"):
            await ScreenReader("127.0.0.1", 1, timeout=0.5).connect()

    run(test)


def test_ppm_and_png() -> None:
    frame = Frame(2, 1, b"\x01\x02\x03\x00\x04\x05\x06\x00")
    assert frame.ppm() == b"P6\n2 1\n255\n\x01\x02\x03\x04\x05\x06"
    png = frame.png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    ihdr = png[8:33]
    assert ihdr[4:8] == b"IHDR" and struct.unpack(">II", ihdr[8:16]) == (2, 1)
    start = png.index(b"IDAT") + 4
    (length,) = struct.unpack(">I", png[start - 8 : start - 4])
    assert zlib.decompress(png[start : start + length]) == b"\x00\x01\x02\x03\x04\x05\x06"
    assert png.endswith(b"IEND\xaeB`\x82")
    assert frame.digest() != Frame(2, 1, bytes(8)).digest()


def test_a_thumbnail_is_small_and_keeps_the_picture() -> None:
    w, h = 1600, 900
    px = bytearray(w * h * 4)
    for y in range(h):
        for x in range(800, w):  # the right half is white
            px[(y * w + x) * 4 : (y * w + x) * 4 + 3] = b"\xff\xff\xff"
    thumb = Frame(w, h, bytes(px)).thumbnail()
    assert (thumb.width, thumb.height, len(thumb.rgbx)) == (200, 112, 200 * 112 * 4)
    left, right = (60 * 200 + 50) * 4, (60 * 200 + 150) * 4
    assert thumb.rgbx[left : left + 3] == b"\x00\x00\x00" and thumb.rgbx[right : right + 3] == b"\xff\xff\xff"
    assert Frame(32, 18, bytes(32 * 18 * 4)).thumbnail(16).height == 9
