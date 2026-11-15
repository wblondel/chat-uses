import pytest

from chatuses.keymap import KeyCombo, char_keysym


def test_combo_with_modifiers() -> None:
    combo = KeyCombo.parse("ctrl+alt+t")
    assert combo.modifiers == (0xFFE3, 0xFFE9)
    assert combo.key == ord("t")
    assert combo.text == "ctrl+alt+t"


@pytest.mark.parametrize(
    ("spec", "modifiers", "key"),
    [
        ("alt+f4", (0xFFE9,), 0xFFC1),
        ("CTRL+C", (0xFFE3,), ord("c")),
        ("super", (), 0xFFEB),  # a modifier alone opens the menu
        ("enter", (), 0xFF0D),
        ("ctrl+shift+esc", (0xFFE3, 0xFFE1), 0xFF1B),
        ("ctrl+plus", (0xFFE3,), ord("+")),
        ("shift+f12", (0xFFE1,), 0xFFC9),
        ("ctrl+é", (0xFFE3,), 0xE9),
    ],
)
def test_combos(spec: str, modifiers: tuple[int, ...], key: int) -> None:
    combo = KeyCombo.parse(spec)
    assert (combo.modifiers, combo.key) == (modifiers, key)


@pytest.mark.parametrize(
    "spec",
    [
        "ctrl+",
        "+",
        "",
        "ctrl++",
        "ctrl+ctrl+x",
        "ctrl+x+ctrl",
        "nonsense+x",
        "ctrl+alt+shift+super+x",  # four modifiers
        "ctrl+notakey",
        "ctrl+\U0001f600",
        "ctrl+\u202e",
        "f13",
    ],
)
def test_bad_combos_are_refused_without_repeating_them(spec: str) -> None:
    with pytest.raises(ValueError) as err:
        KeyCombo.parse(spec)
    for word in ("notakey", "nonsense"):
        assert word not in str(err.value)


@pytest.mark.parametrize(
    ("ch", "keysym"),
    [("a", 0x61), ("Z", 0x5A), ("~", 0x7E), (" ", 0x20), ("é", 0xE9), ("Ç", 0xC7), ("ÿ", 0xFF)],
)
def test_typeable_characters(ch: str, keysym: int) -> None:
    assert char_keysym(ch) == keysym


@pytest.mark.parametrize(
    "ch",
    ["\n", "\t", "\x00", "\x7f", "\u00a0", "\u00d7", "\u00f7", "\u200b", "\u202e", "\U0001f600", "\u20ac"],
)
def test_untypeable_characters(ch: str) -> None:
    assert char_keysym(ch) is None
