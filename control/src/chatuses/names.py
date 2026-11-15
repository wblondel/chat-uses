"""Display names as they may appear on stream.

Names come from YouTube and Discord, where their owners pick them. Before
one is shown it loses what could break the overlay or hide what it says:
control and format characters (bidi overrides, zero-width joiners), and
runs of whitespace. Long names are cut. Phase 5's filter (PLAN.md 7.4)
decides whether a name may be shown at all; this only makes it safe to draw.
"""

import unicodedata

MAX_NAME = 24  # characters shown


def display_name(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw)
    kept = "".join(
        " " if ch.isspace() else ch for ch in text if unicodedata.category(ch)[0] != "C" or ch.isspace()
    )
    name = " ".join(kept.split())
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 1].rstrip() + "…"
    return name or "?"
