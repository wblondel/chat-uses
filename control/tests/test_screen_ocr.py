import asyncio
import os
import stat
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fake_vnc import FakeVnc

from chatuses.safety import screen_ocr
from chatuses.safety.screen_ocr import Hit, OcrError, ScreenWatch, read_screen, tesseract
from chatuses.screen import Frame


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def frame(n: int = 0) -> Frame:
    return Frame(2, 1, bytes([n]) * 8)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def watch(texts: list[str], clock: Clock | None = None) -> tuple[ScreenWatch, list[Hit], list[Frame]]:
    """A watch whose OCR returns the next text for each frame it reads."""
    hits: list[Hit] = []
    read: list[Frame] = []

    async def ocr(f: Frame) -> str:
        read.append(f)
        return texts.pop(0)

    w = ScreenWatch(
        connect=lambda: read_screen("127.0.0.1", 1),
        ocr=ocr,
        matches=lambda text: "badword" in text.replace(" ", "").lower(),
        on_hit=hits.append,
        clock=clock or Clock(),
    )
    return w, hits, read


def test_listed_words_alone_or_run_on_across_lines() -> None:
    async def test() -> None:
        w, hits, _ = watch(["hello\n  a BADWORD here \nbye\n", "some bad\nword spread\nok"])
        hit = await w.check(frame(1))
        assert hit is not None and hit.found == ("a BADWORD here",) and hit.lines == ("a BADWORD here",)
        hit = await w.check(frame(2))  # a terminal wrapped it
        assert hit is not None and hit.found == ("some bad / word spread",)
        assert hit.lines == ("some bad", "word spread")
        assert [h.frame for h in hits] == [frame(1), frame(2)]
        assert w.status()["reads"] == 2 and w.status()["hits"] == 2

    run(test)


def test_an_unchanged_screen_is_not_read_again() -> None:
    async def test() -> None:
        w, _, read = watch(["nothing", "nothing"])
        await w.check(frame(1))
        await w.check(frame(1))
        assert len(read) == 1
        await w.check(frame(2))
        assert len(read) == 2

    run(test)


def test_the_same_line_is_reported_again_only_after_a_while() -> None:
    async def test() -> None:
        clock = Clock()
        w, hits, _ = watch(["x badword"] * 3, clock)
        await w.check(frame(1))
        clock.now += 60
        assert await w.check(frame(2)) is None  # the clock moved on screen; the line didn't
        clock.now += screen_ocr.REPEAT
        assert await w.check(frame(3)) is not None
        assert len(hits) == 2

    run(test)


def test_cleared_lines_stay_quiet_while_on_screen() -> None:
    async def test() -> None:
        clock = Clock()
        w, hits, _ = watch(["x badword", "x badword", "gone", "x badword"], clock)
        await w.check(frame(1))
        w.clear(["x badword"])  # a moderator resumed: it's fine
        clock.now += screen_ocr.REPEAT + 1
        assert await w.check(frame(2)) is None
        await w.check(frame(3))  # it left the screen
        assert await w.check(frame(4)) is not None  # and came back: reported again
        assert len(hits) == 2

    run(test)


def test_the_watch_reads_the_real_screen_and_reconnects() -> None:
    async def test() -> None:
        server = await FakeVnc(width=2, height=1).start()
        paused = False
        hits: list[Hit] = []
        reads: list[Frame] = []

        async def ocr(f: Frame) -> str:
            reads.append(f)
            return "badword"

        w = ScreenWatch(
            connect=lambda: read_screen("127.0.0.1", server.port),
            ocr=ocr,
            matches=lambda text: "badword" in text,
            on_hit=hits.append,
            paused=lambda: paused,
            interval=0.01,
        )
        task = asyncio.create_task(w.run())
        async with asyncio.timeout(2):
            while not hits:
                await asyncio.sleep(0.01)
        assert hits[0].frame.rgbx == b"\x10\x20\x30\x00" * 2
        paused = True
        requests = len(server.requests)
        await asyncio.sleep(0.1)
        assert len(server.requests) == requests  # nothing read while the stream is covered
        paused = False
        await server.hang_up()
        server.fill = b"\x50\x60\x70\x00"  # a new screen after the reconnection
        async with asyncio.timeout(2):
            while len(reads) < 2:
                await asyncio.sleep(0.01)
        assert server.connections >= 2 and reads[-1].rgbx == b"\x50\x60\x70\x00" * 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    run(test)


def fake_tesseract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    exe = tmp_path / "tesseract"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_tesseract_gets_the_frame_and_one_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tesseract(
        tmp_path,
        monkeypatch,
        f'head -c 2 > {tmp_path}/got; cat >/dev/null; echo "$*"; echo "threads=$OMP_THREAD_LIMIT"\n',
    )
    text = asyncio.run(tesseract(frame(), "eng+ara"))
    assert text.splitlines() == ["stdin stdout -l eng+ara --psm 11", "threads=1"]
    assert (tmp_path / "got").read_bytes() == b"P6"  # the frame, as a PPM


def test_tesseract_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tesseract(tmp_path, monkeypatch, "cat >/dev/null; exit 3\n")
    with pytest.raises(OcrError, match="exit 3"):
        asyncio.run(tesseract(frame()))
    fake_tesseract(tmp_path, monkeypatch, "cat >/dev/null; sleep 5\n")
    with pytest.raises(OcrError, match="took more than"):
        asyncio.run(tesseract(frame(), timeout=0.3))
