"""Keys chat can press, as X11 keysyms (what VNC sends; PLAN.md 6.1).

A combo is modifiers plus one key, written with "+": ``ctrl+alt+t``,
``alt+f4``, ``super``. Typed text maps character by character: printable
ASCII and Latin-1 letters (French accents) have keysyms equal to their code
points, and the target's VNC server adds Shift where needed.
"""

from dataclasses import dataclass

MODIFIERS: dict[str, int] = {
    "ctrl": 0xFFE3,  # Control_L
    "control": 0xFFE3,
    "shift": 0xFFE1,  # Shift_L
    "alt": 0xFFE9,  # Alt_L
    "super": 0xFFEB,  # Super_L
    "win": 0xFFEB,
    "meta": 0xFFEB,
}

NAMED_KEYS: dict[str, int] = {
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "tab": 0xFF09,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "space": 0x0020,
    "bksp": 0xFF08,
    "backspace": 0xFF08,
    "del": 0xFFFF,
    "delete": 0xFFFF,
    "ins": 0xFF63,
    "insert": 0xFF63,
    "home": 0xFF50,
    "end": 0xFF57,
    "pgup": 0xFF55,
    "pageup": 0xFF55,
    "pgdn": 0xFF56,
    "pagedown": 0xFF56,
    "up": 0xFF52,
    "down": 0xFF54,
    "left": 0xFF51,
    "right": 0xFF53,
    "menu": 0xFF67,
    "print": 0xFF61,
    "plus": 0x002B,
    "minus": 0x002D,
    **{f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)},
}

MAX_MODIFIERS = 3


def char_keysym(ch: str) -> int | None:
    """Keysym for one typed character, or None if chat can't type it."""
    code = ord(ch)
    if 0x20 <= code <= 0x7E:
        return code
    if 0xC0 <= code <= 0xFF and code not in (0xD7, 0xF7):  # Latin-1 letters, not x and ÷
        return code
    return None


@dataclass(frozen=True)
class KeyCombo:
    modifiers: tuple[int, ...]
    key: int
    text: str  # normalized spelling, for the overlay

    @classmethod
    def parse(cls, spec: str) -> "KeyCombo":
        """Parse "ctrl+alt+t"; raises ValueError with a message chat can read.

        Messages never repeat the spec itself: they appear on stream.
        """
        parts = spec.lower().split("+")
        if any(not p for p in parts):
            raise ValueError("bad key combo (like ctrl+alt+t)")
        *mods, key = parts
        if len(mods) > MAX_MODIFIERS:
            raise ValueError(f"too many modifiers in a combo (at most {MAX_MODIFIERS})")
        if len(set(mods)) != len(mods) or key in mods:
            raise ValueError("a key appears twice in the combo")
        modifiers: list[int] = []
        for m in mods:
            if m not in MODIFIERS:
                raise ValueError("only ctrl, shift, alt and super can go before +")
            modifiers.append(MODIFIERS[m])
        if key in MODIFIERS:
            keysym = MODIFIERS[key]
        elif key in NAMED_KEYS:
            keysym = NAMED_KEYS[key]
        elif len(key) == 1 and (sym := char_keysym(key)) is not None and key != " ":
            keysym = sym
        else:
            raise ValueError("unknown key: letters, digits, f1-f12, enter, esc, up…")
        return cls(tuple(modifiers), keysym, "+".join([*mods, key]))
