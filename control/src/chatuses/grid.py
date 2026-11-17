"""The click grid (PLAN.md 6.2).

Chat names a screen position as a cell such as ``F7`` (column letter, row
number) or a sub-cell such as ``F7.5``: each cell is split 3x3 and numbered
like a phone keypad, 1 top-left, 5 centre, 9 bottom-right. A bare cell means
its centre.

16:9 screens always get 16 x 9 cells (100 px on the 1600x900 desktop);
others get about 100 px cells. All of it is pure arithmetic.
"""

import re
from dataclasses import dataclass

_CELL = re.compile(r"([a-z])([0-9]{1,2})(?:\.([1-9]))?")


@dataclass(frozen=True)
class Grid:
    width: int = 1600
    height: int = 900

    def __post_init__(self) -> None:
        if self.width < 100 or self.height < 100:
            raise ValueError(f"screen too small for a grid: {self.width}x{self.height}")
        if self.cols > 26:
            raise ValueError(f"screen too wide for lettered columns: {self.width} px")

    @property
    def cols(self) -> int:
        return 16 if self.width * 9 == self.height * 16 else round(self.width / 100)

    @property
    def rows(self) -> int:
        return 9 if self.width * 9 == self.height * 16 else round(self.height / 100)

    @property
    def column_labels(self) -> list[str]:
        return [chr(ord("A") + c) for c in range(self.cols)]

    @property
    def row_labels(self) -> list[str]:
        return [str(r + 1) for r in range(self.rows)]

    def locate(self, ref: str) -> tuple[int, int]:
        """Pixel (x, y) of a cell or sub-cell reference, e.g. "F7" or "f7.5".

        Raises ValueError with a message chat can read. Messages never repeat
        the reference itself: they appear on stream, and chat controls it.
        """
        match = _CELL.fullmatch(ref.strip().lower())
        if not match:
            raise ValueError("not a grid cell (like F7 or F7.5)")
        col = ord(match[1]) - ord("a")
        row = int(match[2]) - 1
        sub = int(match[3] or 5) - 1
        if not (0 <= col < self.cols and 0 <= row < self.rows):
            last = f"{self.column_labels[-1]}{self.rows}"
            raise ValueError(f"that cell is off the screen (A1 to {last})")
        cell_w = self.width / self.cols
        cell_h = self.height / self.rows
        x = (col + (sub % 3 + 0.5) / 3) * cell_w
        y = (row + (sub // 3 + 0.5) / 3) * cell_h
        return int(x), int(y)


@dataclass(frozen=True)
class ShuffledGrid(Grid):
    """The grid with its labels changed places (a chaos modifier, game/chaos.py): chat aims
    by the labels the overlay shows, and they're translated back to the real cells."""

    shown_cols: tuple[str, ...] = ()  # the label shown over each real column
    shown_rows: tuple[str, ...] = ()  # ... and each real row

    def locate(self, ref: str) -> tuple[int, int]:
        match = _CELL.fullmatch(ref.strip().lower())
        if not match:
            raise ValueError("not a grid cell (like F7 or F7.5)")
        col, row = match[1].upper(), match[2]
        if col not in self.shown_cols or row not in self.shown_rows:
            raise ValueError("that cell is off the screen")
        real = self.column_labels[self.shown_cols.index(col)] + self.row_labels[self.shown_rows.index(row)]
        return super().locate(real + (f".{match[3]}" if match[3] else ""))
