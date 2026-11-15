#!/usr/bin/env python3
"""Grab one frame from a VNC server and save it as a PNG.

Usage: vncshot.py HOST [PORT] [-o shot.png]

Minimal RFB client (no authentication, Raw encoding, standard library
only), for checking a target's display. It reads data from a machine chat
controls, so every size is bounded and every read has a deadline. Prints
one JSON line: width, height, desktop name, share of non-black pixels and
an estimate of distinct colours.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
import zlib

MAX_SIDE = 8192
MAX_NAME = 1024
MAX_CUT_TEXT = 1 << 20


class RFBError(RuntimeError):
    pass


class Conn:
    def __init__(self, host: str, port: int, deadline: float):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.deadline = deadline

    def read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise RFBError("timed out")
            self.sock.settimeout(min(left, 5))
            chunk = self.sock.recv(min(n - len(buf), 1 << 20))
            if not chunk:
                raise RFBError("server closed the connection")
            buf += chunk
        return bytes(buf)

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)


def handshake(c: Conn) -> tuple[int, int, str]:
    version = c.read(12)
    if not version.startswith(b"RFB 003."):
        raise RFBError(f"not a VNC server: {version!r}")
    minor = int(version[8:11])
    c.send(b"RFB 003.008\n" if minor >= 8 else version)
    if minor >= 7:
        (count,) = c.unpack(">B")
        if count == 0:
            (length,) = c.unpack(">I")
            raise RFBError("server refused: " + c.read(min(length, 1024)).decode(errors="replace"))
        types = c.read(count)
        if 1 not in types:
            raise RFBError(f"server requires authentication (offers {list(types)})")
        c.send(b"\x01")
    else:
        (sectype,) = c.unpack(">I")
        if sectype != 1:
            raise RFBError(f"server requires authentication (type {sectype})")
    if minor >= 8:  # 3.7 and 3.3 send no SecurityResult for "None"
        (result,) = c.unpack(">I")
        if result != 0:
            raise RFBError("security handshake failed")
    c.send(b"\x01")  # ClientInit: shared session, don't kick other viewers
    width, height = c.unpack(">HH")
    c.read(16)  # server pixel format, replaced below
    (name_len,) = c.unpack(">I")
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE) or name_len > MAX_NAME:
        raise RFBError(f"implausible ServerInit: {width}x{height}, name {name_len} bytes")
    name = c.read(name_len).decode(errors="replace")
    return width, height, name


def grab(c: Conn, width: int, height: int) -> bytes:
    # 32 bpp, depth 24, little-endian true colour: bytes arrive as B, G, R, X.
    c.send(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
    c.send(struct.pack(">BxHi", 2, 1, 0))  # SetEncodings: Raw only
    c.send(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))  # full, non-incremental
    fb = bytearray(width * height * 4)
    while True:
        (msg,) = c.unpack(">B")
        if msg == 0:  # FramebufferUpdate
            _, rects = c.unpack(">BH")
            for _ in range(rects):
                x, y, w, h, enc = c.unpack(">HHHHi")
                if enc != 0:
                    raise RFBError(f"unexpected encoding {enc}")
                if x + w > width or y + h > height:
                    raise RFBError("rectangle outside the screen")
                data = c.read(w * h * 4)
                for row in range(h):
                    start = ((y + row) * width + x) * 4
                    fb[start:start + w * 4] = data[row * w * 4:(row + 1) * w * 4]
            return bytes(fb)
        if msg == 1:  # SetColourMapEntries
            _, _, n = c.unpack(">BHH")
            c.read(n * 6)
        elif msg == 2:  # Bell
            continue
        elif msg == 3:  # ServerCutText
            c.read(3)
            (length,) = c.unpack(">I")
            if length > MAX_CUT_TEXT:
                raise RFBError("clipboard message too large")
            c.read(length)
        else:
            raise RFBError(f"unknown server message {msg}")


def write_png(path: str, width: int, height: int, bgrx: bytes) -> None:
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgrx[2::4]
    rgb[1::3] = bgrx[1::4]
    rgb[2::3] = bgrx[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[r * stride:(r + 1) * stride] for r in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        fh.write(chunk(b"IEND", b""))


def stats(bgrx: bytes) -> tuple[float, int]:
    pixels = len(bgrx) // 4
    step = max(1, pixels // 20000)  # sample about 20k pixels
    nonblack = 0
    colours = set()
    for i in range(0, pixels, step):
        b, g, r = bgrx[i * 4], bgrx[i * 4 + 1], bgrx[i * 4 + 2]
        if r + g + b > 30:
            nonblack += 1
        colours.add((r >> 3, g >> 3, b >> 3))
    samples = len(range(0, pixels, step))
    return round(nonblack / samples, 3), len(colours)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host")
    ap.add_argument("port", nargs="?", type=int, default=5900)
    ap.add_argument("-o", "--output", default="shot.png")
    ap.add_argument("--timeout", type=float, default=20)
    args = ap.parse_args()
    try:
        conn = Conn(args.host, args.port, time.monotonic() + args.timeout)
        width, height, name = handshake(conn)
        frame = grab(conn, width, height)
    except (OSError, RFBError, struct.error) as err:
        print(json.dumps({"error": str(err)}))
        return 1
    write_png(args.output, width, height, frame)
    nonblack, colours = stats(frame)
    print(json.dumps({"width": width, "height": height, "name": name,
                      "nonblack": nonblack, "colours": colours, "png": args.output}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
