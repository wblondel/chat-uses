"""What a chat command means once parsed (PLAN.md 6.1).

Input commands (tier 0) go to the executor, subject to rate limits. Meta
commands go to the referee: the mode meter, votes, the overlay's help card.
"""

from dataclasses import dataclass
from typing import Any, Literal

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


StepCommand = TypeText | PressKeys | Click | Move | Drag | Scroll  # what a macro may hold


@dataclass(frozen=True)
class MacroRun:
    """A macro chat voted in (PLAN.md 6.10): its steps run one after the other, as one command."""

    name: str
    steps: tuple[StepCommand, ...]


InputCommand = TypeText | PressKeys | Click | Move | Drag | Scroll | MacroRun

MetaName = Literal[
    "anarchy",
    "democracy",
    "reboot",
    "reset",
    "newrun",
    "yes",
    "no",
    "veto",
    "revive",
    "rip",
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


@dataclass(frozen=True)
class MacroDefine:
    """!macro NAME = STEP; STEP: a vote to add a macro."""

    name: str
    steps: tuple[StepCommand, ...]


@dataclass(frozen=True)
class MacroRemove:
    """!unmacro NAME: a vote to remove one."""

    name: str


@dataclass(frozen=True)
class Amend:
    """!amend LAW VALUE: a vote to change one of the laws chat may (referee/constitution.py)."""

    law: str  # chat's short name: democracy_window
    value: float


@dataclass(frozen=True)
class Buy:
    """!buy NAME: a chaos modifier for points (game/chaos.py)."""

    key: str


MetaCommand = Meta | Rewind | Load | Save | MacroDefine | MacroRemove | Amend | Buy
Command = InputCommand | MetaCommand


@dataclass(frozen=True)
class ParseError:
    """Looked like a command but isn't valid; `reason` is shown to chat."""

    reason: str


_PLAIN_KEYS = {"enter", "tab", "esc", "space", "bksp", "up", "down", "left", "right"}
_CLICK_WORDS: dict[tuple[Button, bool], str] = {
    ("left", False): "click",
    ("left", True): "dclick",
    ("right", False): "rclick",
    ("middle", False): "mclick",
}


def describe(command: Command) -> str:
    """The command as chat would write it, for the overlay's log.

    Built from the parsed command, never from the raw message, so every
    spelling of one command shows the same way.
    """
    match command:
        case TypeText(text=text):
            return f"!type {text}"
        case PressKeys(combos=(combo,), repeat=repeat) if not combo.modifiers and combo.text in _PLAIN_KEYS:
            return f"!{combo.text}" + (f" {repeat}" if repeat > 1 else "")
        case PressKeys(combos=combos, repeat=repeat):
            return "!key " + " ".join(c.text for c in combos) + (f" (x{repeat})" if repeat > 1 else "")
        case Click(cell=cell, button=button, double=double):
            return f"!{_CLICK_WORDS.get((button, double), 'click')} {cell}"
        case Move(cell=cell):
            return f"!move {cell}"
        case Drag(start_cell=start, end_cell=end):
            return f"!drag {start} {end}"
        case Scroll(direction=direction, amount=amount):
            return f"!scroll {direction}" + (f" {amount}" if amount > 1 else "")
        case Meta(name=name):
            return f"!{name}"
        case Rewind(steps=steps):
            return "!rewind" + (f" {steps}" if steps > 1 else "")
        case Load(name=name):
            return f"!load {name}"
        case Save(name=name):
            return f"!save {name}"
        case MacroRun(name=name):
            return f"!{name}"
        case MacroDefine(name=name, steps=steps):
            return f"!macro {name} = {macro_source(steps)}"
        case MacroRemove(name=name):
            return f"!unmacro {name}"
        case Amend(law=law, value=value):
            return f"!amend {law} {value:g}"
        case Buy(key=key):
            return f"!buy {key}"


def macro_source(steps: tuple[StepCommand, ...]) -> str:
    """A macro's steps as chat writes them, without the "!": "key ctrl+alt+t; type hello"."""
    return "; ".join(describe(step).removeprefix("!") for step in steps)


def describe_stored(data: dict[str, Any]) -> str:
    """describe() for an input command as the store keeps it (store.to_json): the death card, the
    site. Built from the parsed fields, never from chat's message (D32)."""
    try:
        match data.get("type"):
            case "TypeText":
                command: Command = TypeText(str(data["text"]))
            case "PressKeys":
                combos = tuple(
                    KeyCombo(tuple(c["modifiers"]), int(c["key"]), str(c["text"])) for c in data["combos"]
                )
                command = PressKeys(combos, int(data.get("repeat", 1)))
            case "Click":
                command = Click(str(data["cell"]), 0, 0, data.get("button", "left"), bool(data.get("double")))
            case "Move":
                command = Move(str(data["cell"]), 0, 0)
            case "Drag":
                command = Drag(str(data["start_cell"]), (0, 0), str(data["end_cell"]), (0, 0))
            case "Scroll":
                command = Scroll(data["direction"], int(data["amount"]))
            case "MacroRun":
                return f"!{data['name']}"
            case _:
                return "a command"
    except (KeyError, TypeError, ValueError):
        return "a command"
    return describe(command)
