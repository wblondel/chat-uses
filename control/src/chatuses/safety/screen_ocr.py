"""Screen OCR (PLAN.md 7.4): what's on the target's screen, read every few
seconds and looked up in the text filter's slur lists.

Text reaches the screen without going through `!type`: a decoded file
(`echo <base64> | base64 -d`), a web page, a file or window named with the
words in pieces. The rolling buffer can't see those; this can.

Only the slur lists, not personal details: a normal desktop is full of links
(the browser's address bar) and long numbers.

Modes (OCR_MODE in brain):
- `shadow` (the default): a hit is logged and posted in #mod-alerts with the
  screenshot; nothing changes on stream. For learning what it gets wrong.
- `enforce`: a hit is a content hold, a panic that says "Content check".
- `off`.

A frame is read only when it changed. Lines are checked one by one, and each
with the next (a terminal wraps a long line anywhere). A line is reported
again only after REPEAT seconds; lines a moderator cleared, by resuming the
hold they caused, aren't reported while they stay on screen.
"""

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from chatuses.screen import Frame, ScreenReader
from chatuses.vnc import VncError

log = logging.getLogger(__name__)

INTERVAL = 5.0  # seconds between reads
REPEAT = 600.0  # seconds before the same line is reported again
OCR_TIMEOUT = 20.0  # seconds for tesseract on one frame
RECONNECT = (2.0, 30.0)


class OcrError(Exception):
    pass


@dataclass(frozen=True)
class Hit:
    ts: float
    found: tuple[str, ...]  # what matched: a line, or two that run on ("a / b")
    lines: tuple[str, ...]  # the screen lines they come from
    frame: Frame


async def tesseract(frame: Frame, langs: str = "eng", timeout: float = OCR_TIMEOUT) -> str:
    """The text tesseract reads in the frame (one thread, low priority)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "10", "tesseract", "stdin", "stdout", "-l", langs, "--psm", "11",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OMP_THREAD_LIMIT": "1"},
            start_new_session=True,  # its own process group: a timeout kills all of it
        )  # fmt: skip
    except OSError as err:
        raise OcrError(f"can't run tesseract: {err}") from err
    try:
        out, _ = await asyncio.wait_for(proc.communicate(frame.ppm()), timeout)
    except TimeoutError as err:
        await _kill(proc)
        raise OcrError(f"tesseract took more than {timeout:.0f} s") from err
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    if proc.returncode != 0:
        raise OcrError(f"tesseract failed (exit {proc.returncode})")
    return out.decode("utf-8", "replace")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.communicate()  # reads what's left, closes the pipes


async def read_screen(host: str, port: int = 5900) -> ScreenReader:
    reader = ScreenReader(host, port)
    await reader.connect()
    return reader


class ScreenWatch:
    def __init__(
        self,
        connect: Callable[[], Awaitable[ScreenReader]],
        ocr: Callable[[Frame], Awaitable[str]],
        matches: Callable[[str], bool],
        on_hit: Callable[[Hit], None],
        paused: Callable[[], bool] = lambda: False,
        interval: float = INTERVAL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._connect = connect
        self.ocr = ocr
        self.matches = matches
        self.on_hit = on_hit
        self.paused = paused  # a panic or a hold covers the screen: nothing to read
        self.interval = interval
        self.clock = clock
        self.cleared: set[str] = set()
        self._reported: dict[str, float] = {}
        self._last_digest = ""
        self.state = "starting"
        self.reads = 0
        self.hits = 0
        self.last_read = 0.0
        self.last_ms = 0
        self.last_error = ""

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reads": self.reads,
            "hits": self.hits,
            "last_read": self.last_read,
            "last_ms": self.last_ms,
            "last_error": self.last_error,
        }

    def clear(self, lines: Iterable[str]) -> None:
        """A moderator said these are fine: don't report them while they stay on screen."""
        self.cleared |= set(lines)

    async def run(self) -> None:
        """Read the screen every `interval` seconds, reconnecting as needed. Never returns."""
        wait = RECONNECT[0]
        while True:
            try:
                reader = await self._connect()
            except VncError as err:
                self.state, self.last_error = "reconnecting", str(err)
                await asyncio.sleep(wait)
                wait = min(wait * 2, RECONNECT[1])
                continue
            wait = RECONNECT[0]
            self.state = "reading"
            try:
                while True:
                    await asyncio.sleep(self.interval)
                    if self.paused():
                        continue
                    frame = await reader.grab()
                    try:
                        await self.check(frame)
                    except OcrError as err:
                        self.last_error = str(err)
                        log.warning("screen OCR: %s", err)
            except VncError as err:
                self.state, self.last_error = "reconnecting", str(err)
            finally:
                await reader.close()

    async def check(self, frame: Frame) -> Hit | None:
        """Read one frame; report what's new and listed."""
        digest = frame.digest()
        if digest == self._last_digest:
            return None
        start = time.monotonic()
        text = await self.ocr(frame)
        self._last_digest = digest
        now = self.clock()
        self.reads += 1
        self.last_read, self.last_ms = now, round((time.monotonic() - start) * 1000)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        self.cleared &= set(lines)  # cleared lines that left the screen may be reported again
        found = self.find(lines)
        self._reported = {k: t for k, t in self._reported.items() if now - t < REPEAT}
        fresh = [f for f in found if " / ".join(f) not in self._reported]
        for f in found:
            self._reported[" / ".join(f)] = now
        if not fresh:
            return None
        self.hits += 1
        sources = tuple(dict.fromkeys(line for f in fresh for line in f))
        hit = Hit(now, tuple(" / ".join(f) for f in fresh), sources, frame)
        self.on_hit(hit)
        return hit

    def find(self, lines: list[str]) -> list[tuple[str, ...]]:
        """The lines that hold a listed word, alone or run on into the next."""
        found: list[tuple[str, ...]] = []
        for i, line in enumerate(lines):
            if line in self.cleared:
                continue
            if self.matches(line):
                found.append((line,))
                continue
            following = lines[i + 1] if i + 1 < len(lines) else ""
            joined = following and following not in self.cleared and not self.matches(following)
            if joined and self.matches(line + following):
                found.append((line, following))
        return found
