"""Chat message → command (PLAN.md 6.1). Strict: anything off-grammar is
refused with a reason, never guessed at.

A message is a command only if it starts with "!" followed by letters, so
"!!!" or "!?" stay ordinary chat. The whole message is first normalized
(Unicode NFKC: "!type" in fullwidth letters reads as "!type") and capped in length.
Arguments are checked against the limits in PLAN.md; typed text may only
hold characters the target can type, which rules out control characters,
zero-width tricks and bidi overrides. Content filtering (PLAN.md 7.4) comes
after parsing, in the pipeline.
"""

import re
import unicodedata
from collections.abc import Callable

from chatuses.commands import (
    Button,
    Click,
    Command,
    Drag,
    Load,
    Meta,
    MetaName,
    Move,
    ParseError,
    PressKeys,
    Rewind,
    Save,
    Scroll,
    TypeText,
)
from chatuses.grid import Grid
from chatuses.keymap import NAMED_KEYS, KeyCombo, char_keysym

MAX_MESSAGE = 200  # characters, after normalization
MAX_TYPE = 40  # characters per !type
MAX_COUNT = 10  # !bksp, arrows, !scroll
MAX_COMBOS = 4  # per !key
MAX_REWIND = 10

_WORD = re.compile(r"[a-z]+")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")

_SINGLE_KEYS = {"enter": "enter", "tab": "tab", "esc": "esc", "space": "space"}
_REPEAT_KEYS = {"bksp": "bksp", "up": "up", "down": "down", "left": "left", "right": "right"}
_CLICKS: dict[str, tuple[Button, bool]] = {
    "click": ("left", False),
    "rclick": ("right", False),
    "dclick": ("left", True),
    "mclick": ("middle", False),
}
_METAS: dict[str, MetaName] = {
    name: name
    for name in (
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
    )
}
_LATER = {"macro": "Phase 9", "amend": "Phase 9"}

Handler = Callable[[str, list[str], str, Grid], Command | ParseError]


def parse(message: str, grid: Grid) -> Command | ParseError | None:
    """Parse one chat message. None means ordinary chat, not a command."""
    text = unicodedata.normalize("NFKC", message).strip()
    if not text.startswith("!"):
        return None
    word, _, rest = text[1:].partition(" ")
    word = word.casefold()
    if not _WORD.fullmatch(word):
        return None
    if len(text) > MAX_MESSAGE:
        return ParseError(f"message too long (at most {MAX_MESSAGE} characters)")
    rest = rest.strip()
    args = rest.split()
    handler = _HANDLERS.get(word)
    if handler is None:
        if word in _LATER:
            return ParseError(f"!{word} isn't available yet ({_LATER[word]})")
        return ParseError(f"unknown command !{word} (try !help)")
    return handler(word, args, rest, grid)


def _count(args: list[str], word: str, maximum: int = MAX_COUNT) -> int | ParseError:
    if not args:
        return 1
    if len(args) > 1 or not args[0].isascii() or not args[0].isdigit():
        return ParseError(f"!{word} takes one number at most, like !{word} 3")
    n = int(args[0])
    if not 1 <= n <= maximum:
        return ParseError(f"!{word}: the number must be 1 to {maximum}")
    return n


def _type(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if not rest:
        return ParseError("!type needs some text, like !type hello")
    if len(rest) > MAX_TYPE:
        return ParseError(f"!type text is too long (at most {MAX_TYPE} characters)")
    for ch in rest:
        if char_keysym(ch) is None:
            return ParseError(f"can't type {unicodedata.name(ch, f'U+{ord(ch):04X}').lower()}")
    return TypeText(rest)


def _single(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if args:
        return ParseError(f"!{word} takes nothing after it")
    return PressKeys((KeyCombo((), NAMED_KEYS[_SINGLE_KEYS[word]], word),))


def _repeat(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    n = _count(args, word)
    if isinstance(n, ParseError):
        return n
    return PressKeys((KeyCombo((), NAMED_KEYS[_REPEAT_KEYS[word]], word),), repeat=n)


def _key(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if not args:
        return ParseError("!key needs a combo, like !key ctrl+alt+t")
    if len(args) > MAX_COMBOS:
        return ParseError(f"!key takes at most {MAX_COMBOS} combos")
    try:
        return PressKeys(tuple(KeyCombo.parse(a) for a in args))
    except ValueError as err:
        return ParseError(str(err))


def _cell(grid: Grid, ref: str) -> tuple[str, tuple[int, int]] | ParseError:
    try:
        return ref.upper(), grid.locate(ref)
    except ValueError as err:
        return ParseError(str(err))


def _click(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if rest.startswith('"'):
        return ParseError("clicking on text isn't available yet (Phase 9)")
    if len(args) != 1:
        return ParseError(f"!{word} needs one cell, like !{word} F7 or !{word} F7.5")
    cell = _cell(grid, args[0])
    if isinstance(cell, ParseError):
        return cell
    button, double = _CLICKS[word]
    name, (x, y) = cell
    return Click(name, x, y, button, double)


def _move(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if len(args) != 1:
        return ParseError("!move needs one cell, like !move K3.9")
    cell = _cell(grid, args[0])
    if isinstance(cell, ParseError):
        return cell
    name, (x, y) = cell
    return Move(name, x, y)


def _drag(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if len(args) != 2:
        return ParseError("!drag needs two cells, like !drag B2 H6")
    start = _cell(grid, args[0])
    if isinstance(start, ParseError):
        return start
    end = _cell(grid, args[1])
    if isinstance(end, ParseError):
        return end
    return Drag(start[0], start[1], end[0], end[1])


def _scroll(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if not args or args[0].casefold() not in ("up", "down"):
        return ParseError("!scroll needs up or down, like !scroll down 3")
    direction = "up" if args[0].casefold() == "up" else "down"
    n = _count(args[1:], word)
    if isinstance(n, ParseError):
        return n
    return Scroll(direction, n)


def _meta(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if args:
        return ParseError(f"!{word} takes nothing after it")
    return Meta(_METAS[word])


def _rewind(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    n = _count(args, word, MAX_REWIND)
    if isinstance(n, ParseError):
        return n
    return Rewind(n)


def _named(word: str, args: list[str], rest: str, grid: Grid) -> Command | ParseError:
    if len(args) != 1:
        return ParseError(f"!{word} needs one name, like !{word} desktop-clean")
    name = args[0].casefold()
    if not _NAME.fullmatch(name):
        return ParseError("names are 1 to 24 letters, digits, - or _")
    return Save(name) if word == "save" else Load(name)


_HANDLERS: dict[str, Handler] = {
    "type": _type,
    **dict.fromkeys(_SINGLE_KEYS, _single),
    **dict.fromkeys(_REPEAT_KEYS, _repeat),
    "key": _key,
    **dict.fromkeys(_CLICKS, _click),
    "move": _move,
    "drag": _drag,
    "scroll": _scroll,
    **dict.fromkeys(_METAS, _meta),
    "rewind": _rewind,
    "save": _named,
    "load": _named,
}
