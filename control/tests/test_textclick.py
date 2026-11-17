"""Clicking on text: !click "Settings" (Phase 9 step 5, textclick.py).

The fixtures are tesseract's real output on a 1600x900 desktop drawn in
Chromium (a top bar, desktop icons, a settings dialog): ocr_desk_plain.tsv is
the frame as it is, ocr_desk_inked.tsv the frame thresholded, and
ocr_desk_boxes.json the true middle of each label, from the page's layout.
"""

import asyncio
import json
import os
import stat
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fake_vnc import FakeVnc

from chatuses import executor as executor_module
from chatuses import textclick
from chatuses.brain import Brain
from chatuses.commands import Click, ClickText, ParseError, TypeText, describe, describe_stored
from chatuses.executor import Executor, Job, JobResult, connect_to
from chatuses.grid import Grid
from chatuses.parser import MAX_CLICK_TEXT, parse
from chatuses.safety.screen_ocr import OcrError
from chatuses.screen import Frame
from chatuses.store import Store, to_json
from chatuses.textclick import Word, find, nth, parse_tsv, read_words, thresholded

FIXTURES = Path(__file__).parent / "fixtures"
PLAIN = (FIXTURES / "ocr_desk_plain.tsv").read_text()
INKED = (FIXTURES / "ocr_desk_inked.tsv").read_text()
TRUTH: list[tuple[str, int, int]] = [
    tuple(b) for b in json.loads((FIXTURES / "ocr_desk_boxes.json").read_text())
]
WORDS = parse_tsv(PLAIN, 0) + parse_tsv(INKED, 1)
GRID = Grid()


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


# --- reading the screen ----------------------------------------------------------------------


def test_tesseract_s_words_are_kept_with_their_boxes_and_lines() -> None:
    plain = parse_tsv(PLAIN, 0)
    assert plain and all(w.text.strip() == w.text and w.width > 0 and w.height > 0 for w in plain)
    assert {w.line[0] for w in plain} == {0}
    assert parse_tsv("level\tpage\n5\t1\t1\t1\t1\t1\t10\t10\t5\t5\t-1\tghost\n", 0) == []  # no confidence
    assert parse_tsv("junk\n5\tx\n", 0) == []


@pytest.mark.parametrize(("label", "x", "y"), [t for t in TRUTH if t != ("Apply", 1336, 836)])
def test_each_label_is_found_where_it_is(label: str, x: int, y: int) -> None:
    spots = find(WORDS, label)
    assert any(abs(sx - x) <= 8 and abs(sy - y) <= 8 for sx, sy in spots), (label, spots)


def test_two_readings_find_what_one_misses() -> None:
    assert find(parse_tsv(PLAIN, 0), "Apply") == []  # small dark text on a grey button
    assert find(WORDS, "Apply")  # thresholded, it reads


def test_matches_are_in_reading_order_each_spot_once() -> None:
    settings = find(WORDS, "Settings")
    assert len(settings) == 2  # both readings saw each, once is kept
    (top_x, top_y), (_, dialog_y) = settings
    assert top_y < dialog_y and (top_x, top_y) == pytest.approx((1481, 15), abs=8)
    assert nth(settings, 2) == settings[1] and nth(settings, 3) is None and nth(settings, 0) is None


def test_case_and_punctuation_around_words_don_t_matter_but_words_do() -> None:
    assert find(WORDS, "file system") == find(WORDS, "File System") == find(WORDS, "“File” System…")
    assert find(WORDS, "refresh RATE")
    assert find(WORDS, "System File") == []  # in order, on one line
    assert find(WORDS, "Nope") == [] and find(WORDS, "...") == []


def test_a_phrase_is_one_line_s_consecutive_words() -> None:
    line = (0, 1, 1, 1)
    words = [Word("Open", 10, 10, 40, 10, line), Word("File", 60, 10, 30, 10, line)]
    words.append(Word("File", 10, 50, 30, 10, (0, 1, 1, 2)))
    assert find(words, "open file") == [(50, 15)]  # the middle of both words
    assert find(words, "file") == [(75, 15), (25, 55)]


def test_the_thresholded_frame_is_black_and_white() -> None:
    frame = Frame(2, 1, bytes([0, 200, 0, 0, 255, 20, 255, 0]))  # green 200: white; green 20: black
    assert thresholded(frame) == b"P4\n2 1\n\x40"  # 01, padded to a byte: 1 is black
    wide = Frame(9, 2, b"\x00\x00\x00\x00" * 18)
    assert thresholded(wide) == b"P4\n9 2\n\xff\x80\xff\x80"  # each row its own bytes


def fake_tesseract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inked: str = "ok") -> None:
    """A tesseract that answers with the fixtures: the plain one for a PPM, the inked one for a
    PBM (or fails, or hangs, for the PBM)."""
    (tmp_path / "plain.tsv").write_text(PLAIN)
    (tmp_path / "inked.tsv").write_text(INKED)
    pgm = {"ok": f"cat {tmp_path}/inked.tsv", "fail": "exit 3", "hang": "sleep 5"}[inked]
    exe = tmp_path / "tesseract"
    exe.write_text(
        "#!/bin/sh\nm=$(head -c 2); cat >/dev/null\n"
        f'case "$*" in *tsv*) ;; *) exit 9;; esac\n'
        f'if [ "$m" = P6 ]; then cat {tmp_path}/plain.tsv; else {pgm}; fi\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


DESK = Frame(1600, 900, b"\x80\x80\x80\x00" * 1600 * 900)


def test_both_readings_run_and_their_words_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tesseract(tmp_path, monkeypatch)
    words = asyncio.run(read_words(DESK))
    assert words == WORDS


def test_a_reading_that_fails_leaves_the_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tesseract(tmp_path, monkeypatch, "fail")
    assert asyncio.run(read_words(DESK)) == parse_tsv(PLAIN, 0)
    monkeypatch.setattr(textclick, "READ_TIMEOUT", 0.3)
    fake_tesseract(tmp_path, monkeypatch, "hang")
    started = time.monotonic()
    assert asyncio.run(read_words(DESK)) == parse_tsv(PLAIN, 0)
    assert time.monotonic() - started < 3


def test_no_reading_at_all_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "tesseract"
    exe.write_text("#!/bin/sh\ncat >/dev/null; exit 3\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(OcrError):
        asyncio.run(read_words(DESK))


# --- what chat types ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ('!click "Settings"', ClickText("Settings")),
        ('!dclick "Trash"#2', ClickText("Trash", 2, "left", True)),
        ("!rclick “File System”", ClickText("File System", 1, "right")),  # a phone's quotes
        ('!mclick "  two   spaces "', ClickText("two spaces", 1, "middle")),
        ('!CLICK "ok"#9', ClickText("ok", 9)),
    ],
)
def test_clicking_on_text_parses(message: str, expected: ClickText) -> None:
    assert parse(message, GRID) == expected


@pytest.mark.parametrize(
    "message",
    [
        '!click ""',
        '!click "   "',
        '!click "x"#0',
        '!click "x"#10',
        '!click "x" and more',
        '!click "unclosed',
        '!click "' + "a" * (MAX_CLICK_TEXT + 1) + '"',
        '!macro mm = click "Settings"',  # a macro aims at cells: what's on screen changes
    ],
)
def test_clicking_on_text_refuses(message: str) -> None:
    assert isinstance(parse(message, GRID), ParseError)


def test_it_is_described_as_typed_and_stored_whole() -> None:
    for command, text in [
        (ClickText("Settings", 2, "right"), '!rclick "Settings"#2'),
        (ClickText("Trash", 1, "left", True), '!dclick "Trash"'),
    ]:
        assert describe(command) == text
        assert describe_stored(json.loads(to_json(command))) == text


# --- its turn comes ----------------------------------------------------------------------------

Reports = list[tuple[JobResult, object]]


async def executor_with(
    resolve: executor_module.Resolve,
) -> tuple[FakeVnc, Executor, Reports, asyncio.Task[None]]:
    server = await FakeVnc().start()
    reports: Reports = []
    executor = Executor(
        lambda: connect_to("127.0.0.1", server.port),
        report=lambda job, result, _s: reports.append((result, job.command)),
        resolve=resolve,
    )
    task = asyncio.create_task(executor.run())
    async with asyncio.timeout(2):
        await executor.online.wait()
    return server, executor, reports, task


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.005)


def test_the_executor_finds_it_when_its_turn_comes_and_clicks_there() -> None:
    async def test() -> None:
        asked: list[ClickText] = []

        async def resolve(command: ClickText) -> Click | None:
            asked.append(command)
            return Click("F7", 550, 650, command.button, command.double) if command.text == "here" else None

        server, executor, reports, task = await executor_with(resolve)
        assert executor.submit(Job("yt:a", TypeText("x")))
        assert executor.submit(Job("yt:a", ClickText("here", 1, "right")))
        assert executor.submit(Job("yt:a", ClickText("gone")))
        await until(lambda: len(reports) == 3)
        assert reports == [
            ("done", TypeText("x")),
            ("done", Click("F7", 550, 650, "right")),  # the record says where it landed
            ("missed", ClickText("gone")),
        ]
        assert [c.text for c in asked] == ["here", "gone"]
        events = await server.wait_for(5)
        assert events[2:] == [("ptr", 0, 550, 650), ("ptr", 4, 550, 650), ("ptr", 0, 550, 650)]
        task.cancel()
        await server.stop()

    run(test)


def test_a_screen_too_slow_to_read_is_a_miss_and_the_queue_moves_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_module, "RESOLVE_TIMEOUT", 0.05)

    async def test() -> None:
        async def resolve(_command: ClickText) -> Click | None:
            await asyncio.sleep(5)
            return None

        server, executor, reports, task = await executor_with(resolve)
        executor.submit(Job("yt:a", ClickText("slow")))
        executor.submit(Job("yt:a", TypeText("y")))
        await until(lambda: len(reports) == 2)
        assert [r for r, _c in reports] == ["missed", "done"]
        task.cancel()
        await server.stop()

    run(test)


def test_without_a_screen_reader_nothing_is_ever_guessed() -> None:
    async def test() -> None:
        assert await executor_module.nowhere(ClickText("x")) is None
        server, executor, reports, task = await executor_with(executor_module.nowhere)
        executor.submit(Job("yt:a", ClickText("Settings")))
        await until(lambda: len(reports) == 1)
        assert reports == [("missed", ClickText("Settings"))]
        assert server.events == []
        task.cancel()
        await server.stop()

    run(test)


# --- through brain -----------------------------------------------------------------------------


def brain_on(store: Store, frame: Frame | None, words: list[Word] | Exception) -> Brain:
    async def grab() -> Frame | None:
        return frame

    async def read(_frame: Frame, langs: str) -> list[Word]:
        assert langs == "eng+ara"
        if isinstance(words, Exception):
            raise words
        return words

    brain = Brain(store, lambda: connect_to("127.0.0.1", 1), grab=grab)
    brain.ocr_langs = "eng+ara"
    brain.__dict__["read"] = read
    return brain


@pytest.fixture
def read_with(monkeypatch: pytest.MonkeyPatch) -> Callable[[Brain], None]:
    def use(brain: Brain) -> None:
        monkeypatch.setattr(textclick, "read_words", brain.__dict__["read"])

    return use


def test_brain_aims_in_grid_coordinates_whatever_the_screen_size(read_with: Callable[[Brain], None]) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            big = Frame(3200, 1800, b"")  # twice the grid: what tesseract saw is halved
            line = (0, 1, 1, 1)
            brain = brain_on(
                store, big, [Word("OK", 1080, 1280, 40, 40, line), Word("OK", 20, 20, 40, 40, line)]
            )
            read_with(brain)
            assert await brain.aim_text(ClickText("ok")) == Click("A1", 20, 20)
            assert await brain.aim_text(ClickText("ok", 2, "left", True)) == Click(
                "F7", 550, 650, "left", True
            )
            assert await brain.aim_text(ClickText("ok", 3)) is None
            assert await brain.aim_text(ClickText("cancel")) is None

    run(test)


def test_chaos_s_mouse_works_on_a_text_click_too(read_with: Callable[[Brain], None]) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store, Frame(1600, 900, b""), [Word("OK", 30, 30, 40, 40, (0, 1, 1, 1))])
            read_with(brain)
            assert (await brain.admin(["chaos", "start", "mirror"]))[0] == "ok"
            assert brain.chaos.transform(ClickText("ok")) == ClickText("ok")  # aimed later, not now
            assert await brain.aim_text(ClickText("ok")) == Click("A1", 1549, 849)  # as any mirrored click

    run(test)


def test_a_screen_that_can_t_be_read_is_a_miss(read_with: Callable[[Brain], None]) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store, Frame(1600, 900, b""), OcrError("tesseract failed (exit 3)"))
            read_with(brain)
            assert await brain.aim_text(ClickText("ok")) is None
            brain.grab = None
            assert await brain.aim_text(ClickText("ok")) is None
            no_screen = brain_on(store, None, [])
            read_with(no_screen)
            assert await no_screen.aim_text(ClickText("ok")) is None

    run(test)


def test_brain_takes_it_filters_it_and_says_when_it_missed(read_with: Callable[[Brain], None]) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_on(store, Frame(1600, 900, b""), [])
            read_with(brain)
            brain.executor.online.set()
            sent: list[dict[str, Any]] = []
            brain.overlay._send = sent.append  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
            for user in ("console:alice", "console:bob"):
                store.seen_user(user, user, time.time(), frozenset())
            brain.filter.kind = lambda text: "hate" if "nasty" in text else None  # type: ignore[method-assign]
            assert (await brain.console_message("bob", '!click "nasty"'))[0] == "filtered"
            assert await brain.console_message("alice", '!click "Settings"#2') == ("accepted", None)
            job = brain.executor.queue.get_nowait()
            assert job.command == ClickText("Settings", 2)
            [line] = [m for m in sent if m["type"] == "log" and m.get("text") == '!click "Settings"#2']
            brain._executed(job, "missed", 0.5)  # pyright: ignore[reportPrivateUsage]
            state = sent[-1]
            assert state == {
                "type": "log-state",
                "id": line["id"],
                "state": "dropped",
                "reason": "not on the screen",
            }
            [event] = [e for e in store.events() if e[2] == "executed"]
            assert event[3]["result"] == "missed"

    run(test)
