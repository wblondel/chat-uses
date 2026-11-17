"""Clicking on text (PLAN.md 6.1, Phase 9): `!click "Settings"`, `!dclick "Trash"#2`.

When its turn comes, brain reads the screen as it is then (a frame from VNC),
twice: as it is, and thresholded to black and white (tesseract misses small
dark text on grey buttons, and finds it there). The words of both readings
are merged. The text chat named is looked for, word by word, on each line:
case and the punctuation around words don't matter. Matches are in reading
order (top to bottom, then left to right) and `#n` picks the n-th; the click
lands on the middle of it. Not on the screen: nothing is clicked, and chat is
told so.

What the screen says is the target's, and hostile: it decides only where a
click lands, never what is shown or typed. The worst a lying screen can do is
a misclick. Chat's quoted text goes through the text filter like typed text.
"""

import asyncio
import string
from dataclasses import dataclass

from chatuses.safety.screen_ocr import OcrError, run_tesseract
from chatuses.screen import Frame

THRESHOLD = 140  # green above this is white: thresholded, small dark text on grey reads
SAME_SPOT = 12  # pixels: a word found by both readings is one match
_INK = bytes(ord("0") if v > THRESHOLD else ord("1") for v in range(256))  # PBM: 1 is black
READ_TIMEOUT = 10.0  # seconds for one reading (a desktop takes well under one)
_PUNCT = string.punctuation + "\u201c\u201d\u2018\u2019\u2026\u00b7\u2022\u2013\u2014"  # quotes, dots, dashes


@dataclass(frozen=True)
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, int, int, int]  # (reading, block, paragraph, line): one line's words share it


def parse_tsv(tsv: str, reading: int = 0) -> list[Word]:
    """tesseract's TSV output: its words (level 5), in its order."""
    found: list[Word] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) != 12 or cols[0] != "5" or not cols[11].strip():
            continue
        try:
            block, par, line = int(cols[2]), int(cols[3]), int(cols[4])
            left, top, width, height = (int(c) for c in cols[6:10])
            confidence = float(cols[10])
        except ValueError:
            continue
        if confidence < 0:
            continue
        found.append(Word(cols[11].strip(), left, top, width, height, (reading, block, par, line)))
    return found


def thresholded(frame: Frame) -> bytes:
    """The frame in black and white, as a 1-bit PBM tesseract reads (the green channel stands
    for brightness: no image library, C speed). 1 bit a pixel, not 8: an 8-bit image of only
    black and white goes through tesseract's own thresholding again, and loses the buttons."""
    bits = frame.rgbx[1::4].translate(_INK)
    width, pad = frame.width, b"0" * (-frame.width % 8)
    row = (width + 7) // 8
    rows = (int(bits[y * width : (y + 1) * width] + pad, 2).to_bytes(row) for y in range(frame.height))
    return f"P4\n{width} {frame.height}\n".encode() + b"".join(rows)


async def read_words(frame: Frame, langs: str = "eng") -> list[Word]:
    """Both readings' words (the two at once; one that fails leaves the other's).
    Raises OcrError if neither could be read."""
    readings = await asyncio.gather(
        run_tesseract(frame.ppm(), langs, ["tsv"], READ_TIMEOUT),
        run_tesseract(thresholded(frame), langs, ["tsv"], READ_TIMEOUT),
        return_exceptions=True,
    )
    words: list[Word] = []
    failed: list[OcrError] = []
    for i, reading in enumerate(readings):
        if isinstance(reading, OcrError):
            failed.append(reading)
        elif isinstance(reading, BaseException):
            raise reading
        else:
            words += parse_tsv(reading, i)
    if len(failed) == len(readings):
        raise failed[0]
    return words


def nth(spots: list[tuple[int, int]], n: int) -> tuple[int, int] | None:
    """The n-th match (1 = the first), or None."""
    return spots[n - 1] if 0 < n <= len(spots) else None


def _norm(text: str) -> str:
    return text.strip(_PUNCT).casefold()


def find(words: list[Word], phrase: str) -> list[tuple[int, int]]:
    """Where the phrase is: the middle of each match, in reading order, each spot once."""
    tokens = [t for t in (_norm(w) for w in phrase.split()) if t]
    if not tokens:
        return []
    lines: dict[tuple[int, int, int, int], list[Word]] = {}
    for word in words:
        lines.setdefault(word.line, []).append(word)
    spots: list[tuple[int, int, int, int]] = []  # (top, left, x, y)
    for line in lines.values():
        texts = [_norm(w.text) for w in line]
        for i in range(len(line) - len(tokens) + 1):
            if texts[i : i + len(tokens)] != tokens:
                continue
            span = line[i : i + len(tokens)]
            left = min(w.left for w in span)
            top = min(w.top for w in span)
            right = max(w.left + w.width for w in span)
            bottom = max(w.top + w.height for w in span)
            x, y = (left + right) // 2, (top + bottom) // 2
            if all(abs(x - sx) > SAME_SPOT or abs(y - sy) > SAME_SPOT for _t, _l, sx, sy in spots):
                spots.append((top, left, x, y))
    spots.sort(key=lambda s: (s[0] // SAME_SPOT, s[1]))
    return [(x, y) for _top, _left, x, y in spots]
