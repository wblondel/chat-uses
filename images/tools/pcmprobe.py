#!/usr/bin/env python3
"""Read a target's raw PCM audio stream and report its rate and loudness.

Usage: pcmprobe.py HOST [PORT] [--seconds 2]

Expects s16le, 48 kHz, stereo (chat-audio.service on port 4712). Waits up to
5 s for the first data (PulseAudio starts sending about a second after a
connection), then reads for --seconds. Reads from a machine chat controls, so
the amount read is capped. Prints one JSON line: steady-state bytes per second
(192000 expected), delay before the first data, and RMS level in dBFS (silence
is -200 here; a full-scale sine is about -3).
"""

from __future__ import annotations

import argparse
import array
import json
import math
import socket
import sys
import time

MAX_BYTES = 16 << 20


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host")
    ap.add_argument("port", nargs="?", type=int, default=4712)
    ap.add_argument("--seconds", type=float, default=2.0)
    args = ap.parse_args()
    data = bytearray()
    first_at = None  # PulseAudio sends nothing for about a second after connecting
    first_len = 0
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
        start = time.monotonic()
        while len(data) < MAX_BYTES:
            now = time.monotonic()
            end = (first_at + args.seconds) if first_at is not None else (start + 5)
            if now >= end:
                break
            sock.settimeout(max(0.05, end - now))
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            if first_at is None:
                first_at = time.monotonic()
                first_len = len(chunk)
            data += chunk
        last_at = time.monotonic()
        sock.close()
    except OSError as err:
        print(json.dumps({"error": str(err)}))
        return 1
    # Steady-state rate: bytes after the first chunk, over the time since it came.
    steady = (len(data) - first_len) / (last_at - first_at) if first_at and last_at > first_at else 0
    usable = len(data) - len(data) % 2
    samples = array.array("h")
    samples.frombytes(bytes(data[:usable]))
    if sys.byteorder == "big":
        samples.byteswap()
    if samples:
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        dbfs = round(20 * math.log10(rms / 32768), 1) if rms > 0 else -200.0
    else:
        dbfs = None
    print(json.dumps({"bytes": len(data), "bytes_per_s": round(steady),
                      "first_data_s": round(first_at - start, 2) if first_at else None,
                      "rms_dbfs": dbfs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
