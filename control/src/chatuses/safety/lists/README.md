# Word lists for the text filter

Offensive terms, kept here so the stream can refuse them (PLAN.md 7.4, D43).
They're never shown anywhere.

Scope: **slurs and hate speech** (D43), not general swearing. One file per
audience language, plus `codes.txt` for language-neutral hate codes.

Syntax (see `../textfilter.py`), one term per line, `#` comments:

| Line | Matches |
|---|---|
| `word` | the whole word, with an optional plural `s`/`es` |
| `*word` | anywhere, even inside other words or spelled out (`w.o r-d`) |
| `two words` | the words in order, with or without separators between them |
| Arabic script | the whole word, with or without a common prefix (ال، و، ب، ل…) |
| `!word` | an ordinary word a `*` term would hit inside it ("snigger"): allowed |

Every spelling trick is handled by the filter, not the lists: case, accents,
look-alike letters, invisible characters, leetspeak, repeated letters. Write
each term once, plainly.

These lists are a starting point. Have native speakers review them, add what
your chat actually uses, and test a new term against ordinary words that
contain it (`uv run pytest tests/test_textfilter.py`) before adding it: a
`*` term inside a common word blocks that word for everyone.
