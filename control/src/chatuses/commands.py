"""What a chat command means once parsed (PLAN.md 6.1).

Input commands (tier 0) go to the executor, subject to rate limits. Meta
commands go to the referee: the mode meter, votes, the overlay's help card.
"""

from dataclasses import dataclass
from typing import Literal

from chatuses.keymap import KeyCombo

Button = Literal["left", "right", "middle"]


@dataclass(frozen=True)
class TypeText:
    text: str


@dataclass(frozen=True)
class PressKeys:
    """Key combos pressed in order, the whole sequence `repeat` times."""

    combos: tuple[KeyCombo, ...]
    repeat: int = 1


@dataclass(frozen=True)
class Click:
    cell: str
    x: int
    y: int
    button: Button = "left"
    double: bool = False


@dataclass(frozen=True)
class Move:
    cell: str
    x: int
    y: int


@dataclass(frozen=True)
class Drag:
    start_cell: str
    start: tuple[int, int]
    end_cell: str
    end: tuple[int, int]


@dataclass(frozen=True)
class Scroll:
    direction: Literal["up", "down"]
    amount: int


InputCommand = TypeText | PressKeys | Click | Move | Drag | Scroll

MetaName = Literal[
    "anarchy",
    "democracy",
    "reboot",
    "reset",
    "newrun",
    "yes",
    "no",
    "veto",
    "impeach",
    "grid",
    "help",
    "rules",
]


@dataclass(frozen=True)
class Meta:
    name: MetaName


@dataclass(frozen=True)
class Rewind:
    steps: int


@dataclass(frozen=True)
class Load:
    name: str


@dataclass(frozen=True)
class Save:
    name: str


MetaCommand = Meta | Rewind | Load | Save
Command = InputCommand | MetaCommand


@dataclass(frozen=True)
class ParseError:
    """Looked like a command but isn't valid; `reason` is shown to chat."""

    reason: str
