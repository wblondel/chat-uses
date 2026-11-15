#!/usr/bin/env python3
"""Fail on invisible or control characters in tracked text files (run by CI).

Trojan Source (CVE-2021-42574) and friends: bidi controls and zero-width
characters make text read differently than it is, in an editor and in a
review. Tests that need them write them as escapes ("\\u202e"). Ruff only
catches some of them, and only in Python.
"""

import subprocess
import sys

BAD = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}  # bidi
BAD |= {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}  # zero-width
BAD |= set(range(0x00, 0x20)) - {0x09, 0x0A, 0x0D}  # control characters but tab and newlines

found = 0
files = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.split(b"\0")
for name in filter(None, files):
    try:
        with open(name, "rb") as f:
            text = f.read().decode("utf-8")
    except (UnicodeDecodeError, IsADirectoryError):
        continue  # binary
    for n, line in enumerate(text.splitlines(), 1):
        hits = sorted({f"U+{ord(c):04X}" for c in line if ord(c) in BAD})
        if hits:
            print(f"{name.decode()}:{n}: {', '.join(hits)}")
            found += 1
print(f"{found} lines with invisible or control characters" if found else "no invisible characters")
sys.exit(1 if found else 0)
