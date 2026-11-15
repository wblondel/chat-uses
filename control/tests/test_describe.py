import pytest

from chatuses.commands import ParseError, describe
from chatuses.grid import Grid
from chatuses.names import display_name
from chatuses.parser import parse


@pytest.mark.parametrize(
    ("message", "shown"),
    [
        ("!type hello world", "!type hello world"),
        ("!TYPE  Déjà vu", "!type Déjà vu"),
        ("!enter", "!enter"),
        ("!bksp 3", "!bksp 3"),
        ("!bksp", "!bksp"),
        ("!key CTRL+Alt+T", "!key ctrl+alt+t"),
        ("!key alt+f4 enter", "!key alt+f4 enter"),
        ("!click f7.5", "!click F7.5"),
        ("!dclick A1", "!dclick A1"),
        ("!rclick h5", "!rclick H5"),
        ("!mclick B2", "!mclick B2"),
        ("!move K3.9", "!move K3.9"),
        ("!drag b2 h6", "!drag B2 H6"),
        ("!scroll DOWN 3", "!scroll down 3"),
        ("!scroll up", "!scroll up"),
        ("!grid", "!grid"),
        ("!rewind 2", "!rewind 2"),
        ("!save Desktop-Clean", "!save desktop-clean"),
        ("!load base", "!load base"),
    ],
)
def test_commands_are_shown_in_one_canonical_form(message: str, shown: str) -> None:
    command = parse(message, Grid())
    assert command is not None and not isinstance(command, ParseError)
    assert describe(command) == shown


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Alice", "Alice"),
        ("  two   spaces\tand\nlines ", "two spaces and lines"),
        ("evil\u202eright-to-left", "evilright-to-left"),
        ("zero\u200bwidth\u2066isolate", "zerowidthisolate"),
        ("bell\x07 and nul\x00", "bell and nul"),
        ("\uff21\uff4c\uff49\uff43\uff45", "Alice"),  # fullwidth letters
        ("\u200b\u200b", "?"),
        ("n" * 24, "n" * 24),
        ("n" * 25, "n" * 23 + "…"),
    ],
)
def test_display_names_are_safe_to_draw(raw: str, shown: str) -> None:
    assert display_name(raw) == shown
