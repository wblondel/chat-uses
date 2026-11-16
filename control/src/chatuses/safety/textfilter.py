"""The text filter (PLAN.md 7.4, D43): what chat may put on stream in words.

Applied to text typed into the target (!type), save names, and the names
shown on the overlay. It refuses:

- slurs and hate speech, from the word lists in lists/ (English, French,
  Spanish, Italian, Arabic, and hate codes). General swearing is allowed.
- personal details: email addresses, phone numbers, street addresses, links.

Matching works on a canonical form of the text, so the usual tricks don't
help: Unicode compatibility forms (fullwidth, styled letters), case, accents
and other combining marks, invisible characters, look-alike Cyrillic and
Greek letters, leetspeak (4 and @ read as a, 3 as e, 1 ! | and l as i...),
repeated letters. Terms marked * also match inside words and spelled out
with separators ("w.o r-d"). Typed text is checked together with the last
BUFFER characters typed (backspaces applied), so a word typed in pieces,
by one person or several, is refused when the last piece arrives.

Regular expressions use possessive quantifiers only: no input can make them
backtrack for long. Refusal reasons are fixed sentences that never repeat
the text (D32).
"""

import hashlib
import re
import unicodedata
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Literal

from chatuses.names import display_name

LISTS = Path(__file__).parent / "lists"
BUFFER = 200  # characters of typed text remembered

HATE = "that's not allowed on stream"
PERSONAL = "no personal details on stream (emails, phone numbers, addresses, links)"

Kind = Literal["hate", "personal"]

# Look-alike letters from other scripts, read as the Latin letter they mimic
# (display names can be in any script; !type only types Latin-1).
_CONFUSABLES = {
    # Cyrillic
    0x0430: "a", 0x0432: "b", 0x0435: "e", 0x0451: "e", 0x043A: "k", 0x043C: "m", 0x043D: "h",
    0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0442: "t", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0457: "i", 0x0458: "j", 0x0455: "s", 0x0501: "d", 0x04CF: "l", 0x0261: "g", 0x0131: "i",
    # Greek
    0x03B1: "a", 0x03B2: "b", 0x03B5: "e", 0x03B7: "n", 0x03B9: "i", 0x03BA: "k", 0x03BD: "v",
    0x03BF: "o", 0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x03C7: "x", 0x03B3: "y",
}  # fmt: skip
# Arabic spelling variants read as one (hamza forms fall away with the marks).
_ARABIC = {0x0649: chr(0x064A), 0x0629: chr(0x0647)}  # alef maqsura -> ya, ta marbuta -> ha
_TATWEEL = chr(0x0640)  # the stretching stroke: decoration
_FOLD = str.maketrans(_CONFUSABLES | _ARABIC)
# Leetspeak and near-identical letters, folded into one letter each. Symbols
# are leet only inside a word ("sp!c"); at its edges they're punctuation
# ("spic!"), so text is matched twice: symbols folded, and symbols kept.
_LEET_ALNUM = {"4": "a", "8": "b", "3": "e", "9": "g", "6": "g", "1": "i", "l": "i", "0": "o", "5": "s",
               "z": "s", "7": "t", "v": "u"}  # fmt: skip
_LEET_SYMBOLS = {"@": "a", "(": "c", "!": "i", "|": "i", "$": "s", "+": "t"}
_LEET = str.maketrans(_LEET_ALNUM | _LEET_SYMBOLS)
_LEET_KEEP_SYMBOLS = str.maketrans(_LEET_ALNUM)

_SEP = r"[\W_]"  # anything between letters: spaces, punctuation
_START = r"(?<![^\W_])"  # a word starts here
_END = r"(?![^\W_])"  # a word ends here
_ARABIC_PREFIXES = "|".join(
    re.escape("".join(map(chr, p)))
    for p in [(0x0648, 0x0627, 0x0644), (0x0628, 0x0627, 0x0644), (0x0644, 0x0644),
              (0x0627, 0x0644), (0x0648,), (0x0628,), (0x0644,)]
)  # fmt: skip  # wal-, bil-, lil-, al-, wa-, bi-, li-


def normalize(text: str) -> str:
    """Compatibility forms folded, lower case, no marks or invisible characters, look-alikes read as Latin."""
    text = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text).casefold())
    kept = (c for c in text if unicodedata.category(c) not in ("Mn", "Me", "Cf") and c != _TATWEEL)
    return "".join(kept).translate(_FOLD)


def canonical(text: str) -> str:
    """normalize(), then leetspeak folded: what word lists are matched against."""
    return normalize(text).translate(_LEET)


def canonical_forms(text: str) -> tuple[str, str]:
    """The two canonical forms text is matched in: symbols folded, symbols kept as punctuation."""
    n = normalize(text)
    return n.translate(_LEET), n.translate(_LEET_KEEP_SYMBOLS)


def _is_arabic(text: str) -> bool:
    return any(0x0600 <= ord(c) <= 0x06FF for c in text)


def _letters(word: str, spaced: bool) -> str:
    """Each run of a letter matches that many or more; spaced: with separators anywhere."""
    parts: list[str] = []
    for ch, run in groupby(word):
        n, c = len(list(run)), re.escape(ch)
        if spaced:
            parts.append(f"(?:{c}{_SEP}*+){{{n},}}+")
        else:
            parts.append(f"{c}{{{n},}}+")
    return "".join(parts)


def _pattern(line: str) -> tuple[str, bool]:
    """A list line as a regex, and whether it matches anywhere (True) or as whole words."""
    if line.startswith("*"):
        return _letters(canonical(line[1:]).replace(" ", ""), spaced=True), True
    words = [_letters(w, spaced=False) for w in canonical(line).split()]
    body = f"{_SEP}*+".join(words)
    if _is_arabic(line):
        # The prefix may give its letters back: some terms start with them.
        return f"{_START}(?:{_ARABIC_PREFIXES})?{body}{_END}", False
    return f"{_START}{body}(?:e?s)?+{_END}", False


@dataclass(frozen=True)
class WordLists:
    words: re.Pattern[str]  # whole words and phrases
    anywhere: re.Pattern[str]  # * terms
    allowed: frozenset[str]  # ! words: ordinary words a * term would hit

    @classmethod
    def load(cls, folder: Path = LISTS) -> "WordLists":
        return cls.parse(
            line for f in sorted(folder.glob("*.txt")) for line in f.read_text("utf-8").splitlines()
        )

    @classmethod
    def parse(cls, lines: Iterable[str]) -> "WordLists":
        words: list[str] = []
        anywhere: list[str] = []
        allowed: set[str] = set()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):
                allowed.add(canonical(line[1:]))
                continue
            pattern, is_anywhere = _pattern(line)
            (anywhere if is_anywhere else words).append(pattern)
        return cls(_compile(words), _compile(anywhere), frozenset(allowed))

    def count(self, text: str) -> int:
        """How many times the lists match in text's canonical forms, added up: only compared
        before and after a piece of typing, where a sum can't hide a new match in either form."""
        n = 0
        for c in canonical_forms(text):
            n += sum(1 for _ in self.words.finditer(c))
            n += sum(
                1
                for m in self.anywhere.finditer(c)
                if _word_around(c, m.start(), m.end()) not in self.allowed
            )
        return n


def _compile(patterns: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns) or r"(?!)")


_WORD_CHAR = re.compile(r"[^\W_]")
_TRAILING_SEPS = re.compile(rf"{_SEP}+$")


def _word_around(text: str, start: int, end: int) -> str:
    """The whole word a match sits in ("" if the match spans separators)."""
    core = _TRAILING_SEPS.sub("", text[start:end])  # a spaced match swallows separators after it
    if re.search(_SEP, core):
        return ""
    end = start + len(core)
    while start > 0 and _WORD_CHAR.match(text[start - 1]):
        start -= 1
    while end < len(text) and _WORD_CHAR.match(text[end]):
        end += 1
    return text[start:end]


# --- personal details --------------------------------------------------------------

_TLDS = (
    "com|net|org|info|io|gg|tv|me|co|uk|fr|es|it|de|be|ch|ca|us|ru|cn|xyz|app|dev|ly|link|live|"
    "site|online|shop|ma|dz|tn|eg|sa|ae|eu|biz|ai"
)
_STREET_FIRST = (  # the street word before the name: "12 rue de la Paix", "via Roma 10"
    "rue|avenue|av|boulevard|bd|chemin|impasse|allee|place|quai|route|calle|avenida|plaza|paseo|camino"
    "|via|viale|piazza|corso|largo|vicolo"
)
_STREET_LAST = "street|st|road|rd|avenue|ave|lane|ln|drive|dr|court|ct|way|boulevard|blvd"  # "12 Main Street"
_PERSONAL = re.compile(
    "|".join(
        [
            r"[\w.+-]++@[\w-]++(?:\.[\w-]++)*+\.[a-z]{2,}",  # email
            r"(?:https?://|www\.)\S++",  # link
            rf"\b[a-z0-9-]{{2,}}+\.(?:{_TLDS})\b",  # bare domain
            r"(?<!\d)\+?\d(?:[\s.()-]{0,2}+\d){8,}+(?!\d)",  # phone number: 9 digits or more
            # Street addresses: bounded repeats only (at most 3 words), so no long backtracking.
            rf"\b\d{{1,5}}(?:\s*(?:bis|ter))?\s*,?\s*(?:{_STREET_FIRST})\b",
            rf"\b\d{{1,5}}\s+(?:[a-z]+\s+){{1,3}}?(?:{_STREET_LAST})\b",
            rf"\b(?:{_STREET_FIRST})\s+(?:[a-z]+\s+){{0,3}}?\d{{1,5}}\b",
        ]
    )
)
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def personal_count(text: str) -> int:
    """How many personal details text holds (an IPv4 address isn't one: chat types them in game)."""
    return sum(1 for m in _PERSONAL.finditer(normalize(text)) if not _IPV4.fullmatch(m.group().strip()))


# --- the filter ----------------------------------------------------------------------


class TextFilter:
    def __init__(self, lists: WordLists | None = None) -> None:
        self.lists = lists or WordLists.load()
        self.buffer: deque[str] = deque(maxlen=BUFFER)

    def kind(self, text: str) -> Kind | None:
        """What's wrong with text on its own, if anything."""
        if self.lists.count(text):
            return "hate"
        if personal_count(text):
            return "personal"
        return None

    def check(self, text: str) -> str | None:
        """The reason to refuse text on its own (a save name), or None."""
        kind = self.kind(text)
        return None if kind is None else (HATE if kind == "hate" else PERSONAL)

    def typed_kind(self, text: str) -> Kind | None:
        """What typing text would put on screen, counting what was typed just before."""
        before = "".join(self.buffer)
        if self.lists.count(before + text) > self.lists.count(before):
            return "hate"
        if personal_count(before + text) > personal_count(before):
            return "personal"
        return None

    def check_typed(self, text: str) -> str | None:
        kind = self.typed_kind(text)
        return None if kind is None else (HATE if kind == "hate" else PERSONAL)

    # What was typed, as far as the screen goes: called for accepted commands.

    def typed(self, text: str) -> None:
        self.buffer.extend(text)

    def key(self, name: str, times: int = 1) -> None:
        """A named key: backspace takes characters back, enter/tab/space separate words."""
        for _ in range(times):
            if name == "bksp":
                if self.buffer:
                    self.buffer.pop()
            elif name in ("enter", "tab", "space"):
                self.buffer.append("\n" if name == "enter" else " ")

    def forget(self) -> None:
        """The screen changed completely (a new or rewound target): start over."""
        self.buffer.clear()

    def safe_name(self, user_id: str, raw: str) -> str:
        """The name to show on the overlay: anonymized if it holds anything refused."""
        name = display_name(raw)
        if self.kind(raw) or self.kind(name):
            return f"viewer#{int(hashlib.sha256(user_id.encode()).hexdigest()[:8], 16) % 10000:04d}"
        return name
