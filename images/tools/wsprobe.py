#!/usr/bin/env python3
"""Listen to a websocket feed and print what arrives.

Usage: wsprobe.py ws://HOST:PORT/PATH [--seconds N] [--until-type TYPE]

Minimal websocket client (RFC 6455, text frames only, standard library
only), for checking brain's overlay feed from the host or from bcast, where
there is no websocket library. Prints each text message as one line, then
exits after N seconds, or as soon as a JSON message of type TYPE arrives
(exit 0), or with status 1 if the handshake fails or nothing arrived.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
from urllib.parse import urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1 << 20


class Closed(Exception):
    pass


def recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise Closed("connection closed")
        data += chunk
    return data


def handshake(url: str, timeout: float) -> socket.socket:
    parts = urlsplit(url)
    if parts.scheme != "ws" or not parts.hostname:
        raise SystemExit("only ws://HOST:PORT/PATH URLs")
    sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
    key = base64.b64encode(os.urandom(16))
    request = (
        f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key.decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += recv_exact(sock, 1)
        if len(head) > 16384:
            raise Closed("handshake reply too long")
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status + b" ":
        raise Closed(f"handshake refused: {status.decode(errors='replace')}")
    accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
    if b"sec-websocket-accept: " + accept.lower() not in head.lower():
        raise Closed("bad Sec-WebSocket-Accept")
    return sock


def frames(sock: socket.socket):
    while True:
        b0, b1 = recv_exact(sock, 2)
        opcode, length = b0 & 0x0F, b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", recv_exact(sock, 2))
        elif length == 127:
            (length,) = struct.unpack(">Q", recv_exact(sock, 8))
        if length > MAX_MESSAGE:
            raise Closed("message too large")
        mask = recv_exact(sock, 4) if b1 & 0x80 else b""
        payload = recv_exact(sock, length)
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 8:
            raise Closed("closed by the server")
        if opcode == 1:
            yield payload.decode("utf-8", "replace")


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("url")
    args.add_argument("--seconds", type=float, default=3.0)
    args.add_argument("--until-type", help="stop at the first JSON message of this type")
    opts = args.parse_args()
    deadline = time.monotonic() + opts.seconds
    seen = 0
    try:
        sock = handshake(opts.url, timeout=min(5.0, opts.seconds))
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        for text in frames(sock):
            print(text, flush=True)
            seen += 1
            if opts.until_type:
                try:
                    if json.loads(text).get("type") == opts.until_type:
                        return 0
                except (ValueError, AttributeError):
                    pass
            sock.settimeout(max(0.1, deadline - time.monotonic()))
    except (OSError, Closed) as err:
        if not isinstance(err, TimeoutError):
            print(f"wsprobe: {err}", file=sys.stderr)
            return 1
    if opts.until_type:
        return 1
    return 0 if seen else 1


if __name__ == "__main__":
    sys.exit(main())
