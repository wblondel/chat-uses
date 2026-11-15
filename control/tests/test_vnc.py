import asyncio
import struct
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from fake_vnc import VERSION, FakeVnc, handshake, reason, server_init

from chatuses.keymap import KeyCombo
from chatuses.vnc import VncError, VncInput

T = 0.3  # handshake timeout for hostile servers, seconds


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


async def connected(server: FakeVnc, timeout: float = T) -> VncInput:
    vnc = VncInput("127.0.0.1", server.port, timeout)
    await vnc.connect()
    return vnc


# --- the handshake ----------------------------------------------------------


def test_handshake_as_a_shared_client_with_no_auth() -> None:
    async def test() -> None:
        server = await FakeVnc(width=1280, height=720).start()
        vnc = await connected(server)
        assert (vnc.width, vnc.height, vnc.name) == (1280, 720, "fake")
        assert server.client_replies == [VERSION, b"\x01", b"\x01"]  # 3.8, security None, shared
        await vnc.close()
        await server.stop()

    run(test)


async def refused(script: bytes, match: str) -> None:
    server = await FakeVnc(script=script).start()
    vnc = VncInput("127.0.0.1", server.port, T)
    with pytest.raises(VncError, match=match):
        await vnc.connect()
    assert not vnc.connected
    await server.stop()


@pytest.mark.parametrize(
    ("script", "match"),
    [
        (b"RFB 003.003\n", "RFB 3.8"),
        (b"HTTP/1.1 400\n", "RFB 3.8"),
        (VERSION + b"\x00" + reason(b"too many clients"), "too many clients"),
        (VERSION + b"\x00" + struct.pack(">I", 0xFFFFFFFF), "reason too long"),
        (VERSION + b"\x01\x02", "authentication"),  # VNC password only
        (VERSION + b"\x01\x01" + struct.pack(">I", 1) + reason(b"go away"), "go away"),
        (VERSION + b"\x01\x01" + struct.pack(">I", 0) + server_init(name=b"x" * 2000), "name too long"),
        (VERSION + b"\x01\x01" + struct.pack(">I", 0) + server_init(0, 0), "empty screen"),
        (VERSION + b"\x01", "cannot connect"),  # truncated, then silence
        (b"", "cannot connect"),  # says nothing at all
    ],
)
def test_hostile_handshakes_are_refused(script: bytes, match: str) -> None:
    run(lambda: refused(script, match))


def test_a_huge_name_length_is_refused_without_reading_it() -> None:
    script = VERSION + b"\x01\x01" + struct.pack(">I", 0)
    script += struct.pack(">HH", 1600, 900) + bytes(16) + struct.pack(">I", 0xFFFFFFFF)
    started = time.monotonic()
    run(lambda: refused(script, "name too long"))
    assert time.monotonic() - started < T  # refused at once, not after waiting for 4 GB


def test_refusal_reasons_are_capped() -> None:
    run(lambda: refused(VERSION + b"\x00" + reason(b"y" * 1000), "server refused: y{200}$"))


def test_cancelling_a_connect_closes_the_socket() -> None:
    async def test() -> None:
        server = await FakeVnc(script=b"").start()  # says nothing: the handshake waits
        vnc = VncInput("127.0.0.1", server.port, timeout=5)
        attempt = asyncio.create_task(vnc.connect())
        await asyncio.sleep(0.1)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        await server.wait_hung_up()  # the server saw the socket close
        assert not vnc.connected
        await server.stop()

    run(test)


def test_nobody_listening() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        await server.stop()
        with pytest.raises(VncError, match="cannot connect"):
            await connected(server)

    run(test)


# --- after the handshake ------------------------------------------------------


def test_what_the_server_sends_later_is_ignored() -> None:
    async def test() -> None:
        garbage = bytes(range(256)) * 4096  # 1 MiB: fake framebuffer updates, bells, junk
        server = await FakeVnc(after_init=garbage).start()
        vnc = await connected(server)
        await vnc.key(0x61, True)
        await vnc.key(0x61, False)
        assert await server.wait_for(2) == [("key", True, 0x61), ("key", False, 0x61)]
        assert vnc.connected and server.unknown == []
        await vnc.close()
        await server.stop()

    run(test)


def test_a_hang_up_is_noticed_without_sending() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        vnc = await connected(server)
        await server.hang_up()
        async with asyncio.timeout(1):
            await vnc.lost.wait()
        assert not vnc.connected
        with pytest.raises(VncError, match="not connected"):
            await vnc.key(0x61, True)
        await vnc.close()
        await server.stop()

    run(test)


def test_a_handshake_sent_in_one_burst_works() -> None:
    async def test() -> None:
        server = await FakeVnc(script=handshake(800, 600)).start()
        vnc = await connected(server)
        assert (vnc.width, vnc.height) == (800, 600)
        await vnc.close()
        await server.stop()

    run(test)


# --- input ------------------------------------------------------------------


def test_combo_presses_modifiers_around_the_key() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        vnc = await connected(server)
        await vnc.combo(KeyCombo.parse("ctrl+alt+t"))
        ctrl, alt, t = 0xFFE3, 0xFFE9, 0x74
        assert await server.wait_for(6) == [
            ("key", True, ctrl),
            ("key", True, alt),
            ("key", True, t),
            ("key", False, t),
            ("key", False, alt),
            ("key", False, ctrl),
        ]
        await vnc.close()
        await server.stop()

    run(test)


def test_clicks_drags_and_scrolls() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        vnc = await connected(server)
        await vnc.click(10, 20, 1, count=2)
        await vnc.scroll("down", 2)
        await vnc.drag((100, 100), (200, 300), steps=2)
        await vnc.click(5, 5, 4)
        assert await server.wait_for(17) == [
            ("ptr", 0, 10, 20),
            ("ptr", 1, 10, 20),
            ("ptr", 0, 10, 20),
            ("ptr", 1, 10, 20),
            ("ptr", 0, 10, 20),
            ("ptr", 16, 10, 20),  # wheel down, where the pointer is
            ("ptr", 0, 10, 20),
            ("ptr", 16, 10, 20),
            ("ptr", 0, 10, 20),
            ("ptr", 0, 100, 100),
            ("ptr", 1, 100, 100),
            ("ptr", 1, 150, 200),
            ("ptr", 1, 200, 300),
            ("ptr", 0, 200, 300),
            ("ptr", 0, 5, 5),
            ("ptr", 4, 5, 5),  # right button
            ("ptr", 0, 5, 5),
        ]
        await vnc.close()
        await server.stop()

    run(test)


def test_the_pointer_stays_on_screen() -> None:
    async def test() -> None:
        server = await FakeVnc(width=800, height=600).start()
        vnc = await connected(server)
        await vnc.pointer(5000, -3)
        assert await server.wait_for(1) == [("ptr", 0, 799, 0)]
        await vnc.close()
        await server.stop()

    run(test)


def test_closing_lets_go_of_held_keys_and_buttons() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        vnc = await connected(server)
        await vnc.key(0xFFE3, True)
        await vnc.pointer(50, 60, 1)
        await vnc.close()
        assert await server.wait_for(4) == [
            ("key", True, 0xFFE3),
            ("ptr", 1, 50, 60),
            ("key", False, 0xFFE3),
            ("ptr", 0, 50, 60),
        ]
        await server.stop()

    run(test)
