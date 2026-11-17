import pytest

from chatuses.commands import (
    Click,
    ClickText,
    Drag,
    Load,
    Meta,
    Move,
    ParseError,
    PressKeys,
    Rewind,
    Save,
    Scroll,
    TypeText,
)
from chatuses.grid import Grid
from chatuses.parser import MAX_MESSAGE, MAX_TYPE, parse

GRID = Grid()


def p(text: str) -> object:
    return parse(text, GRID)


def reason(text: str) -> str:
    result = p(text)
    assert isinstance(result, ParseError), f"{text!r} parsed as {result!r}"
    return result.reason


# --- what isn't a command --------------------------------------------------


@pytest.mark.parametrize(
    "text", ["hello", "", "   ", "!!!", "!?", "! type hi", "!123", "!\x00type nul", "!ty\u200bpe hi"]
)
def test_ordinary_chat_is_not_a_command(text: str) -> None:
    assert p(text) is None


# --- !type -------------------------------------------------------------------


def test_type() -> None:
    assert p("!type hello world") == TypeText("hello world")
    assert p("!TYPE Hello") == TypeText("Hello")
    assert p("!type   spaced  out  ") == TypeText("spaced  out")  # edges trimmed, inside kept
    assert p("!type café crème") == TypeText("café crème")
    assert p("!type " + "x" * MAX_TYPE) == TypeText("x" * MAX_TYPE)


def test_fullwidth_is_normalized() -> None:
    assert p("\uff01\uff54\uff59\uff50\uff45 \uff48\uff49") == TypeText("hi")  # fullwidth !type hi


@pytest.mark.parametrize(
    "text",
    [
        "!type",
        "!type    ",
        "!type " + "x" * (MAX_TYPE + 1),
        "!type a\nb",  # no Enter smuggled in
        "!type a\rb",
        "!type a\tb",
        "!type \u202eevil",  # bidi override
        "!type in\u200bvisible",  # zero-width space
        "!type \U0001f600",
        "!type \x1b[2J",
        "!type euro \u20ac",
    ],
)
def test_type_refuses(text: str) -> None:
    reason(text)


def test_too_long_message() -> None:
    assert "too long" in reason("!key " + "a" * MAX_MESSAGE)


# --- keys --------------------------------------------------------------------


@pytest.mark.parametrize("word", ["enter", "tab", "esc", "space"])
def test_single_keys(word: str) -> None:
    result = p(f"!{word}")
    assert isinstance(result, PressKeys) and result.repeat == 1 and len(result.combos) == 1
    reason(f"!{word} 3")


@pytest.mark.parametrize(
    ("text", "repeat"), [("!bksp", 1), ("!bksp 5", 5), ("!down 3", 3), ("!left 10", 10), ("!up \uff15", 5)]
)
def test_repeated_keys(text: str, repeat: int) -> None:
    result = p(text)
    assert isinstance(result, PressKeys) and result.repeat == repeat


@pytest.mark.parametrize(
    "text",
    ["!bksp 0", "!bksp 11", "!bksp -1", "!bksp x", "!bksp 2 3", "!bksp 99999999999999999999", "!bksp \u0665"],
)
def test_repeated_keys_refuse(text: str) -> None:
    reason(text)  # U+0665 is an Arabic-Indic 5: a digit, but not one we take


def test_key() -> None:
    result = p("!key ctrl+alt+t")
    assert isinstance(result, PressKeys) and [c.text for c in result.combos] == ["ctrl+alt+t"]
    result = p("!KEY Ctrl+C ctrl+v alt+tab super")
    assert isinstance(result, PressKeys) and len(result.combos) == 4


@pytest.mark.parametrize(
    "text", ["!key", "!key a b c d e", "!key ctrl+", "!key ctrl+nothing", "!key ctrl+ctrl+c"]
)
def test_key_refuses(text: str) -> None:
    reason(text)


# --- pointer -----------------------------------------------------------------


def test_clicks() -> None:
    assert p("!click F7.5") == Click("F7.5", 550, 650)
    assert p("!click f7") == Click("F7", 550, 650)
    assert p("!rclick A1.1") == Click("A1.1", 16, 16, "right")
    assert p("!dclick C2") == Click("C2", 250, 150, "left", double=True)
    assert p("!mclick P9.9") == Click("P9.9", 1583, 883, "middle")


@pytest.mark.parametrize("text", ["!click", "!click Z1", "!click A1 B2", "!click F7.0", "!dclick 7F"])
def test_clicks_refuse(text: str) -> None:
    reason(text)


def test_click_on_text_is_its_own_command() -> None:
    assert p('!click "Settings"') == ClickText("Settings")  # more in test_textclick.py


def test_move_and_drag() -> None:
    assert p("!move K3.9") == Move("K3.9", 1083, 283)
    assert p("!drag B2 H6") == Drag("B2", (150, 150), "H6", (750, 550))
    for text in ("!move", "!move K3 K4", "!drag B2", "!drag B2 Q9", "!drag B2 H6 A1"):
        reason(text)


@pytest.mark.parametrize(
    ("text", "result"), [("!scroll down 3", Scroll("down", 3)), ("!scroll UP", Scroll("up", 1))]
)
def test_scroll(text: str, result: Scroll) -> None:
    assert p(text) == result


@pytest.mark.parametrize("text", ["!scroll", "!scroll sideways", "!scroll down 11", "!scroll 3 down"])
def test_scroll_refuses(text: str) -> None:
    reason(text)


# --- meta --------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    [
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
    ],
)
def test_meta(word: str) -> None:
    assert p(f"!{word}") == Meta(word)  # type: ignore[arg-type]
    reason(f"!{word} now")


def test_rewind_save_load() -> None:
    assert p("!rewind") == Rewind(1)
    assert p("!rewind 3") == Rewind(3)
    assert p("!save desktop-clean") == Save("desktop-clean")
    assert p("!save Desk_1") == Save("desk_1")
    assert p("!load desktop-clean") == Load("desktop-clean")
    for text in (
        "!rewind 11",
        "!rewind -1",
        "!save",
        "!save a b",
        "!save ../../etc/passwd",
        "!save " + "a" * 25,
        "!load -x",
    ):
        reason(text)


def test_unknown_and_later_commands() -> None:
    assert "unknown command !discord" in reason("!discord")
    assert "needs a law and a number" in reason("!amend quorum")


# --- refusals appear on stream: they must never repeat what chat wrote -------


@pytest.mark.parametrize(
    "text",
    [
        "!click xyzzy",
        "!click F7xyzzy",
        "!move xyzzy",
        "!drag A1 xyzzy",
        "!key ctrl+xyzzy",
        "!key xyzzy+a",
        "!scroll xyzzy",
        "!bksp xyzzy",
        "!save xyzzy/x",
        "!help xyzzy",
        "!enter xyzzy",
        "!type xyzzy\nmore",
        "!type " + "xyzzy" * 20,
        "!macro ab = click xyzzy",
        "!macro xyzzy",
        "!macro x = enter; xyzzy",
        "!unmacro xyzzy/x",
    ],
)
def test_refusals_never_repeat_what_chat_wrote(text: str) -> None:
    assert "xyzzy" not in reason(text).lower()
