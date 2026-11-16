"""The text filter (PLAN.md 7.4, D43), including red-team tests 8 and 9 (7.7).

Evasions are generated from the word lists themselves, so every term in every
list is tested and these tests don't spell the terms out.
"""

import time
from collections.abc import Callable

import pytest

from chatuses.safety.textfilter import HATE, LISTS, PERSONAL, TextFilter, WordLists, canonical

FILTER = TextFilter()
ZERO_WIDTH = chr(0x200B)
SHADDA = chr(0x0651)  # an Arabic mark
TATWEEL = chr(0x0640)
AL = chr(0x0627) + chr(0x0644)  # the Arabic article


def terms(prefix: str = "") -> list[str]:
    """List lines (no comments, no allowed words); prefix "*" for anywhere terms only."""
    lines = [
        line.strip()
        for f in sorted(LISTS.glob("*.txt"))
        for line in f.read_text("utf-8").splitlines()
        if line.strip() and not line.strip().startswith(("#", "!"))
    ]
    return [line for line in lines if line.startswith(prefix)]


def plain(term: str) -> str:
    return term.removeprefix("*")


def is_arabic(text: str) -> bool:
    return any(0x0600 <= ord(c) <= 0x06FF for c in text)


LATIN = [t for t in terms() if not is_arabic(t)]
ARABIC = [t for t in terms() if is_arabic(t)]
LEET = str.maketrans({"a": "4", "e": "3", "i": "1", "o": "0", "s": "5", "t": "7"})
ACCENTS = str.maketrans({"a": "à", "e": "é", "i": "ï", "o": "ô", "u": "ü", "c": "ç"})


def fullwidth(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


def bold(text: str) -> str:  # mathematical bold letters: NFKC turns them back into a-z
    return "".join(chr(0x1D41A + ord(c) - ord("a")) if "a" <= c <= "z" else c for c in text)


EVASIONS: dict[str, Callable[[str], str]] = {
    "as is": lambda t: t,
    "punctuation around": lambda t: f"!{t}!!",
    "symbols inside": lambda t: t.replace("i", "!").replace("a", "@").replace("s", "$"),
    "upper case": str.upper,
    "leetspeak": lambda t: t.translate(LEET),
    "letters repeated": lambda t: "".join(c * 3 if c.isalpha() else c for c in t),
    "zero-width characters": lambda t: ZERO_WIDTH.join(t),
    "fullwidth": fullwidth,
    "styled letters": bold,
    "accents added": lambda t: t.translate(ACCENTS),
    "in a sentence": lambda t: f"well, {t}, said someone.",
}


@pytest.mark.parametrize("evasion", EVASIONS)
@pytest.mark.parametrize("term", LATIN)
def test_every_term_is_refused_however_written(term: str, evasion: str) -> None:
    """Red-team test 8: whole, leetspeak, zero-width characters... (Latin script)."""
    text = EVASIONS[evasion](plain(term))
    assert FILTER.kind(text) == "hate", f"{evasion}: not refused"


@pytest.mark.parametrize("term", terms("*"))
def test_anywhere_terms_are_refused_spelled_out_and_inside_words(term: str) -> None:
    word = plain(term)
    for text in (".".join(word), " ".join(word), "-".join(word).upper(), f"x{word}y", f"{word}lover"):
        assert FILTER.kind(text) == "hate"


@pytest.mark.parametrize("term", [t for t in LATIN if " " not in t and not t.startswith("*")])
def test_whole_word_terms_cover_plurals(term: str) -> None:
    assert FILTER.kind(term + "s") == "hate"


@pytest.mark.parametrize("term", [t for t in LATIN if " " in t])
def test_phrases_are_refused_with_any_separators(term: str) -> None:
    for text in (term.replace(" ", ""), term.replace(" ", "_"), term.replace(" ", " ... ").upper()):
        assert FILTER.kind(text) == "hate"


@pytest.mark.parametrize("term", ARABIC)
def test_arabic_terms_with_marks_stretching_and_prefixes(term: str) -> None:
    """Display names can be in Arabic script (!type can only type Latin letters)."""
    first = term.split()[0]
    variants = [
        term,
        term.replace(first, first[0] + SHADDA + first[1:], 1),  # a vowel mark
        term.replace(first, first[0] + TATWEEL + first[1:], 1),  # a stretched letter
        AL + term if not term.startswith(AL) else term,  # the article
        f"hello {term}!",
    ]
    for text in variants:
        assert FILTER.kind(text) == "hate"


ORDINARY = [
    # Words that contain a term, or look like one, and must stay usable
    "Niger and Nigeria",
    "a niggardly sum, niggardliness",
    "he sniggered, sniggering",
    "spice, spicy, conspicuous",
    "raccoon in a cocoon, tycoon",
    "Pakistan, Japan, Japanese",
    "Scunthorpe, assassin, class",
    "bike, hike, Mike",
    "swop the shop",
    "dykes",  # an old spelling of dikes... is refused: see test below
    "Mongolia",
    "terra, terrazza",
    # Spanish, Italian and French ordinary words near the lists
    "el gato negro",
    "il nero",
    "el ratón",
    "marisco",
    "la jota",
    "en retard",
    "un pédant",
    # General swearing is allowed (D43)
    "fuck this shit",
    "merde alors",
    "joder, hostia",
    "cazzo",
    "putain",
    # Things chat types in the game
    "sudo apt install cowsay",
    "ping 10.20.0.13",
    "curl 151.101.2.132",
    "nano notes.txt; python3 script.py",
    "echo 1234 > /tmp/a",
    "call 911",
    "chocolate-doom -iwad freedoom2.wad",
]


@pytest.mark.parametrize("text", [t for t in ORDINARY if t != "dykes"])
def test_ordinary_text_passes(text: str) -> None:
    assert FILTER.kind(text) is None


def test_some_ordinary_words_are_the_price() -> None:
    # Whole-word slurs that are also rare ordinary words: refused, on purpose.
    assert FILTER.kind("dykes") == "hate"


@pytest.mark.parametrize(
    "text",
    [
        "write to bob@example.com",
        "BOB.SMITH+stream@mail.example.fr",
        "06 12 34 56 78",
        "06.12.34.56.78",
        "+33 6 12 34 56 78",
        "(555) 123-4567 ext",
        "12 rue de la Paix",
        "12 bis, rue des Lilas",
        "123 Main Street",
        "calle Mayor 5",
        "via Roma 10",
        "https://example.com/x",
        "www.example.org",
        "visit example.com now",
        "bob" + ZERO_WIDTH + "@example.com",
    ],
)
def test_personal_details_are_refused(text: str) -> None:
    assert FILTER.kind(text) == "personal"


# --- typing in pieces: the rolling buffer ------------------------------------------------


@pytest.fixture
def fresh() -> TextFilter:
    f = TextFilter(FILTER.lists)
    return f


def type_all(f: TextFilter, pieces: list[str]) -> list[str | None]:
    """Each piece as a separate !type; returns the reasons (None when typed)."""
    reasons: list[str | None] = []
    for piece in pieces:
        reason = f.check_typed(piece)
        reasons.append(reason)
        if reason is None:
            f.typed(piece)
    return reasons


@pytest.mark.parametrize("term", [t for t in LATIN if len(plain(t)) >= 4])
def test_a_term_split_across_three_commands_is_refused(fresh: TextFilter, term: str) -> None:
    """Red-team test 8: split across 3 commands (by one person or several: one shared buffer).

    Sometimes a middle piece is refused already: it completes a shorter term
    ("fagg" holds a shorter slur, "negres" a plural).
    """
    word = plain(term).replace(" ", "")
    a, b = len(word) // 3, 2 * len(word) // 3
    reasons = type_all(fresh, ["echo " + word[:a], word[a:b], word[b:]])
    assert reasons[0] is None and HATE in reasons[1:]


def test_backspaces_are_taken_into_account(fresh: TextFilter) -> None:
    word = plain(terms("*")[0])
    fresh.typed(word[:3] + "x")
    fresh.key("bksp")
    assert fresh.check_typed(word[3:]) == HATE
    fresh.forget()
    fresh.typed(word[:3])
    fresh.key("bksp", 3)  # all gone
    assert fresh.check_typed(word[3:]) is None


def test_a_term_typed_across_enter_is_still_refused(fresh: TextFilter) -> None:
    word = plain(terms("*")[0])
    fresh.typed(word[:3])
    fresh.key("enter")
    assert fresh.check_typed(word[3:]) == HATE  # on screen, the pieces sit one under the other


def test_a_phone_number_typed_in_pieces_is_refused(fresh: TextFilter) -> None:
    assert type_all(fresh, ["echo 06 12", " 34 56", " 78"]) == [None, None, PERSONAL]


def test_the_buffer_forgets_old_text(fresh: TextFilter) -> None:
    word = plain(terms("*")[0])
    fresh.typed(word[:3])
    fresh.typed("x" * 300)  # far away now: the next piece starts a new word anyway
    assert fresh.check_typed(word[3:]) is None
    assert len(fresh.buffer) == 200


def test_ordinary_typing_goes_through(fresh: TextFilter) -> None:
    assert (
        type_all(fresh, ["echo Chat était là", " > /tmp/a", "; ls -la", " # spice and Niger"]) == [None] * 4
    )


# --- names on the overlay: red-team test 9 -----------------------------------------------


@pytest.mark.parametrize("term", LATIN + ARABIC)
def test_a_name_holding_a_term_is_anonymized(term: str) -> None:
    name = FILTER.safe_name("yt:UC123", f"xX {plain(term)}!! Xx")
    assert name.startswith("viewer#") and name == FILTER.safe_name("yt:UC123", "other bad " + plain(term))
    assert name != FILTER.safe_name("yt:UC456", plain(term))  # each person their own number


def test_names_with_personal_details_are_anonymized_and_ordinary_names_kept() -> None:
    assert FILTER.safe_name("dc:1", "call me 0612345678").startswith("viewer#")
    assert FILTER.safe_name("dc:1", "Élodie 🎮") == "Élodie 🎮"
    assert FILTER.safe_name("dc:1", "Cyrillic " + chr(0x0430) + "lice") == "Cyrillic " + chr(0x0430) + "lice"


def test_lookalike_letters_in_names_are_seen_through() -> None:
    word = plain(terms("*")[0])
    disguised = word.replace("e", chr(0x0435)).replace("i", chr(0x0456))  # Cyrillic e and i
    assert disguised != word and FILTER.safe_name("yt:x", disguised).startswith("viewer#")


# --- the rest ----------------------------------------------------------------------------


def test_reasons_never_repeat_the_text() -> None:
    for text in [plain(terms("*")[0]), "bob@example.com"]:
        reason = FILTER.check(text)
        assert reason in (HATE, PERSONAL) and text not in reason


def test_list_syntax() -> None:
    lists = WordLists.parse(["# comment", "", "word", "*inside", "two words", "!insideout"])

    def found(text: str) -> bool:
        return lists.count(text) > 0

    assert (found("a word here"), found("words"), found("swordfish")) == (True, True, False)
    assert (found("xinsidex"), found("i n s i d e"), found("insideout")) == (True, True, False)
    assert (found("two words"), found("twowords"), found("two")) == (True, True, False)


def test_canonical_form() -> None:
    assert canonical("ＨÉ" + ZERO_WIDTH + "1!0") == "heiio"


@pytest.mark.parametrize("text", ["i" * 240, "!|1l" * 60, "n " * 120, "a.b-" * 60, "9" * 240, "@" * 240])
def test_no_input_makes_it_slow(fresh: TextFilter, text: str) -> None:
    fresh.typed(text[:200])
    started = time.perf_counter()
    fresh.check_typed(text[:40])
    FILTER.kind(text)
    assert time.perf_counter() - started < 0.1
