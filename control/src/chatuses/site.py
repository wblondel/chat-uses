"""The public site (PLAN.md Phase 8, D65): static pages about the runs, from brain's store.

brain renders it into SITE_DIR (/var/lib/chat-brain/site, on its data volume)
when something changes (a run starts or ends, lives, an achievement, a
snapshot, a law, a deletion request), at most every SITE_EVERY seconds, and
every SITE_REFRESH seconds anyway (a live run's age, points). Only files whose
bytes changed are written, each atomically, then manifest.json (every file's
size and SHA-256). The host copies the site for Caddy with
infra/host/site_sync.py, which trusts nothing the manifest doesn't vouch for.

Pages: the home page, the Graveyard, a page per run (its last screen, its
timeline, its last commands, achievements and who played most), the
leaderboard, the achievements, The Laws, the rules and the privacy notice.

Everything on it was public already: names are the overlay's (filtered, D43),
commands are written as the overlay writes them (D32), pictures are past
frames of the stream (D55). `tag` escapes whatever it isn't given as Markup,
so text from chat can't become HTML; pages carry no script, and Caddy's
Content-Security-Policy forbids any. The site's own texts (site/pages/*.md in
the repo: about, rules, privacy) are Markdown, rendered by a small converter
that escapes too and keeps only https:, mailto: and the site's own links.
"""

import hashlib
import html
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from chatuses.game.achievements import Achievement
from chatuses.game.runs import uptime_text
from chatuses.referee.laws import ACTIONS, Laws, Ruleset
from chatuses.referee.referee import BUILT
from chatuses.store import Store

log = logging.getLogger(__name__)

SITE_EVERY = 10.0  # seconds between renders, at most, when something changed
SITE_REFRESH = 300.0  # seconds between renders anyway
LEADERBOARD = 100  # names on the leaderboard
IMAGE = re.compile(r"(?:\d{1,9}|run-\d{1,9})\.png")  # thumbs/ files the site may carry
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9 ,.'/-]{2,}\]|\(LINK[^)]*\)")  # left to fill in
PAGES = ("about", "rules", "privacy")  # site/pages/NAME.md
NAV = (
    ("index.html", "Home"),
    ("graveyard.html", "Graveyard"),
    ("leaderboard.html", "Leaderboard"),
    ("achievements.html", "Achievements"),
    ("laws.html", "The Laws"),
    ("rules.html", "Rules"),
)


# --- HTML that escapes by default -----------------------------------------------------------


class Markup(str):
    """HTML built here: safe to put in a page as it is."""

    __slots__ = ()


def esc(value: object) -> Markup:
    return value if isinstance(value, Markup) else Markup(html.escape(str(value), quote=True))


_VOID = frozenset({"img", "br", "hr", "meta", "link"})


def _children(items: Iterable[object]) -> str:
    out: list[str] = []
    for item in items:
        if item is None or item is False:
            continue
        if isinstance(item, Iterable) and not isinstance(item, str):
            out.append(_children(cast(Iterable[object], item)))
        else:
            out.append(esc(item))
    return "".join(out)


def tag(element: str, /, *children: object, **attrs: object) -> Markup:
    """<element attrs>children</element>: text and attribute values escaped, lists flattened.
    `class_` is `class`; an attribute that's None or False is left out, True stands alone."""
    parts = [element]
    for key, value in attrs.items():
        if value is None or value is False:
            continue
        key = key.rstrip("_").replace("_", "-")
        parts.append(key if value is True else f'{key}="{esc(value)}"')
    start = f"<{' '.join(parts)}>"
    if element in _VOID:
        return Markup(start)
    return Markup(f"{start}{_children(children)}</{element}>")


# --- Markdown, a little of it (site/pages/*.md) ---------------------------------------------

_INLINE = re.compile(r"\*\*(?P<bold>.+?)\*\*|`(?P<code>[^`]+)`|\[(?P<text>[^\]]+)\]\((?P<url>[^()\s]+)\)")
_SAFE_URL = re.compile(r"(?:https://|mailto:)[^\s\"'<>]+|[a-z0-9-]+\.html(?:#[a-z0-9-]+)?|#[a-z0-9-]+")
_HEADING = re.compile(r"(?P<level>#{1,4})\s+(?P<text>.+)")
_ITEM = re.compile(r"(?:[-*]|(?P<n>\d{1,3})\.)\s+(?P<text>.*)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)  # notes for whoever edits the page


def inline(text: str) -> Markup:
    """**bold**, `code` and [links](https://…); everything else is text."""
    out: list[str] = []
    at = 0
    for m in _INLINE.finditer(text):
        out.append(esc(text[at : m.start()]))
        if m["bold"] is not None:
            out.append(tag("strong", inline(m["bold"])))
        elif m["code"] is not None:
            out.append(tag("code", m["code"]))
        elif _SAFE_URL.fullmatch(m["url"]):
            out.append(tag("a", inline(m["text"]), href=m["url"]))
        else:  # a link we don't make (javascript:, a path, a placeholder): as typed
            out.append(esc(m[0]))
        at = m.end()
    out.append(esc(text[at:]))
    return Markup("".join(out))


def markdown(text: str) -> Markup:
    """Headings (# to ####), paragraphs, lists (- or 1.), --- rules; inline as `inline`.
    <!-- comments --> are left out."""
    text = _COMMENT.sub("", text)
    blocks: list[Markup] = []
    paragraph: list[str] = []
    items: list[list[str]] = []
    ordered = False

    def flush() -> None:
        if paragraph:
            blocks.append(tag("p", inline(" ".join(paragraph))))
            paragraph.clear()
        if items:
            blocks.append(tag("ol" if ordered else "ul", [tag("li", inline(" ".join(i))) for i in items]))
            items.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif (heading := _HEADING.fullmatch(line)) and not raw[0].isspace():
            flush()
            blocks.append(tag(f"h{len(heading['level'])}", inline(heading["text"])))
        elif line in ("---", "***"):
            flush()
            blocks.append(tag("hr"))
        elif item := _ITEM.fullmatch(line):
            numbered = item["n"] is not None
            if paragraph or (items and numbered != ordered):
                flush()
            ordered = numbered
            items.append([item["text"]])
        elif items:
            items[-1].append(line)  # the item goes on
        else:
            paragraph.append(line)
    flush()
    return Markup("\n".join(blocks))


# --- what the pages show ----------------------------------------------------------------------


@dataclass
class SiteData:
    """Read from brain on its event loop (the store stays on one thread); rendered elsewhere."""

    now: float
    runs: list[dict[str, Any]]  # newest first; the live one has "live": True
    unlocked: dict[int, dict[str, dict[str, Any]]]  # run id -> key -> {"at", "credited"}
    thumbnails: dict[int, list[dict[str, Any]]]  # run id -> its snapshots' thumbnails
    names: dict[str, str]  # user -> their name as the overlay shows it
    leaderboard: list[dict[str, Any]]  # {"name", "points"}, most first
    achievements: list[Achievement]
    ruleset: Ruleset
    laws: Laws
    amended: dict[str, float]  # laws changed over the ruleset's
    lives_left: int | None  # the live run's (None: lives off)
    pictures: frozenset[str] | None = None  # the thumbs/ files the site carries (None: all)

    def shows(self, picture: str | None) -> bool:
        return (
            bool(picture)
            and IMAGE.fullmatch(picture or "") is not None
            and (self.pictures is None or picture in self.pictures)
        )


def gather(
    store: Store,
    *,
    achievements: list[Achievement],
    ruleset: Ruleset,
    laws: Laws,
    safe_name: Callable[[str, str], str],
    lives_left: int | None,
    now: float | None = None,
) -> SiteData:
    now = time.time() if now is None else now
    runs = store.runs(limit=1_000_000)
    for run in runs:
        if run["ended"] is None:
            run["live"] = True
            run["contributors"] = store.contributors(run["started"], now)
            run["last_commands"] = [
                {**c, "name": safe_name(c["user"], c["name"])}
                for c in store.last_inputs(run["started"], now, 10)
            ]
    unlocked = store.all_unlocked()
    wanted: list[str] = []
    for run in runs:
        listed: list[dict[str, Any]] = run["contributors"] or []
        wanted += [str(c["user"]) for c in listed]
    wanted += [user for keys in unlocked.values() for got in keys.values() for user in got["credited"]]
    names = {user: safe_name(user, name) for user, name in store.user_names(wanted).items()}
    board = [
        {"name": safe_name(row["user"], row["name"]), "points": row["points"]}
        for row in store.leaderboard(LEADERBOARD, now)
    ]
    return SiteData(
        now=now,
        runs=runs,
        unlocked=unlocked,
        thumbnails=store.run_thumbnails(),
        names=names,
        leaderboard=board,
        achievements=achievements,
        ruleset=ruleset,
        laws=laws,
        amended=store.laws(),
        lives_left=lives_left,
    )


# --- the pages ---------------------------------------------------------------------------------


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S")


def page(title: str, body: object, up: str = "", description: str = "") -> bytes:
    """A whole page. `up` leads back to the site's root ("../" from runs/)."""
    head = tag(
        "head",
        tag("meta", charset="utf-8"),
        tag("meta", name="viewport", content="width=device-width, initial-scale=1"),
        tag("meta", name="referrer", content="no-referrer"),
        tag("meta", name="description", content=description) if description else None,
        tag("title", f"{title} · chat uses"),
        tag("link", rel="stylesheet", href=f"{up}style.css"),
    )
    body_ = tag(
        "body",
        tag(
            "header",
            tag("a", "CHAT USES", href=f"{up}index.html", class_="brand"),
            tag("nav", [tag("a", label, href=f"{up}{href}") for href, label in NAV]),
        ),
        tag("main", body),
        tag(
            "footer",
            "Everything here was on the stream already. ",
            tag("a", "Privacy", href=f"{up}privacy.html"),
        ),
    )
    return ("<!doctype html>\n" + tag("html", head, body_, lang="en") + "\n").encode()


def _run_link(number: int, up: str = "") -> Markup:
    return tag("a", f"Run #{number}", href=f"{up}runs/{number}.html")


def _lives(n: int) -> str:
    return f"{n} {'life' if n == 1 else 'lives'}"


def _graveyard_table(runs: list[dict[str, Any]], up: str = "") -> Markup:
    rows = [
        tag(
            "tr",
            tag("td", _run_link(run["number"], up)),
            tag("td", when(run["ended"])),
            tag("td", uptime_text(run["ended"] - run["started"])),
            tag("td", run["cause"] or "?"),
            tag("td", run["lives_used"], class_="num"),
            tag("td", run["final_blow"]["name"] if run["final_blow"] else "—"),
        )
        for run in runs
    ]
    head = tag(
        "tr",
        [tag("th", h) for h in ("Run", "Died", "Lived", "How", "Lives used", "Probably killed by")],
    )
    return tag("table", tag("thead", head), tag("tbody", rows), class_="graveyard")


def _facts(run: dict[str, Any], d: SiteData) -> Markup:
    if run.get("live"):
        total = run["lives_start"] + run["lives_bonus"]
        lives = "off" if d.lives_left is None else f"{d.lives_left} of {total}"
        done = len(d.unlocked.get(run["id"], {}))
        rows = [
            ("Alive for", f"{uptime_text(d.now - run['started'])} (at {when(d.now)})"),
            ("Lives left", lives),
            ("Achievements", f"{done} of {len(d.achievements)}"),
        ]
    else:
        rows = [
            ("Died", f"{when(run['ended'])}: {run['cause'] or '?'}"),
            ("Lived", uptime_text(run["ended"] - run["started"])),
            ("Lives used", run["lives_used"]),
        ]
    rows.insert(0, ("Born", when(run["started"])))
    return tag("dl", [[tag("dt", k), tag("dd", v)] for k, v in rows], class_="facts")


def home(d: SiteData, about: Markup) -> bytes:
    parts: list[object] = [tag("section", about, class_="about")]
    live = next((run for run in d.runs if run.get("live")), None)
    if live is not None:
        parts.append(
            tag("section", tag("h2", "Now: ", _run_link(live["number"])), _facts(live, d), class_="now")
        )
    dead = [run for run in d.runs if not run.get("live")]
    if dead:
        parts.append(
            tag(
                "section",
                tag("h2", "Recent deaths"),
                _graveyard_table(dead[:5]),
                tag("p", tag("a", "The whole Graveyard", href="graveyard.html")),
            )
        )
    if d.leaderboard:
        parts.append(
            tag(
                "section",
                tag("h2", "Most points"),
                tag(
                    "ol",
                    [
                        tag("li", row["name"], " ", tag("span", row["points"], class_="pts"))
                        for row in d.leaderboard[:5]
                    ],
                ),
                tag("p", tag("a", "The leaderboard", href="leaderboard.html")),
            )
        )
    return page("Chat controls a computer", parts, description="Chat controls a real computer, live.")


def graveyard(d: SiteData) -> bytes:
    dead = [run for run in d.runs if not run.get("live")]
    body: list[object] = [
        tag("h1", "The Graveyard"),
        tag(
            "p",
            "Every run of the machine that died, newest first. The final blow is a joke, not an accusation.",
        ),
    ]
    body.append(_graveyard_table(dead) if dead else tag("p", "Nothing has died yet.", class_="empty"))
    return page("The Graveyard", body)


def run_page(run: dict[str, Any], d: SiteData) -> bytes:
    number = run["number"]
    body: list[object] = [tag("h1", f"Run #{number}", class_="alive" if run.get("live") else "dead")]
    body.append(_facts(run, d))
    frame = run["final_frame"]
    if d.shows(frame):
        body.append(
            tag(
                "figure",
                tag("img", src=f"../img/{frame}", alt=f"The last screen of run #{number}", loading="lazy"),
                tag("figcaption", "Its last screen"),
                class_="last",
            )
        )
    if blow := run["final_blow"]:
        body.append(
            tag(
                "p",
                "Probably killed by ",
                tag("strong", blow["name"]),
                ": ",
                tag("code", blow["text"]),
                ". A joke, not an accusation.",
                class_="blow",
            )
        )
    if run["last_commands"]:
        body.append(tag("h2", "Last commands"))
        body.append(
            tag(
                "ol",
                [
                    tag(
                        "li",
                        tag("code", c["text"]),
                        " ",
                        tag("span", c["name"], " · ", _clock(c["ts"]), class_="by"),
                    )
                    for c in run["last_commands"]
                ],
                class_="commands",
            )
        )
    names = {a.key: a for a in d.achievements}
    got = d.unlocked.get(run["id"], {})
    if got:
        body.append(tag("h2", "Achievements"))
        items: list[Markup] = []
        for key, info in got.items():
            a = names.get(key)
            credited = [d.names[u] for u in info["credited"] if u in d.names]
            items.append(
                tag(
                    "li",
                    tag("strong", a.name if a else key),
                    f": {a.description}" if a else "",
                    tag("span", f" · {when(info['at'])}", class_="by"),
                    tag("div", "Thanks to ", ", ".join(credited), class_="credit") if credited else None,
                )
            )
        body.append(tag("ul", items, class_="unlocked"))
    listed: list[dict[str, Any]] = run["contributors"] or []
    contributors = [(d.names[c["user"]], int(c["commands"])) for c in listed if c["user"] in d.names]
    if contributors:
        body.append(tag("h2", "Who played most"))
        body.append(
            tag(
                "ol",
                [
                    tag(
                        "li",
                        name,
                        " ",
                        tag("span", f"{n} {'command' if n == 1 else 'commands'}", class_="by"),
                    )
                    for name, n in contributors
                ],
                class_="players",
            )
        )
    shots = [s for s in d.thumbnails.get(run["id"], []) if d.shows(s["thumb"])]
    if shots:
        body.append(tag("h2", "Timeline"))
        body.append(
            tag(
                "div",
                [
                    tag(
                        "figure",
                        tag("img", src=f"../img/{s['thumb']}", alt=_shot_label(s), loading="lazy"),
                        tag("figcaption", _shot_label(s)),
                    )
                    for s in shots
                ],
                class_="timeline",
            )
        )
    return page(f"Run #{number}", body, up="../")


def _shot_label(shot: dict[str, Any]) -> str:
    if shot["kind"] == "named" and shot["name"]:
        return f"save “{shot['name']}” · {when(shot['created'])}"
    return when(shot["created"])


def leaderboard(d: SiteData) -> bytes:
    body: list[object] = [
        tag("h1", "Leaderboard"),
        tag(
            "p",
            "A point a minute you're active in chat, two for each command that runs (ten a minute at most), "
            "and the points of the achievements you helped unlock. ",
            tag("code", "/me"),
            " on Discord tells you yours.",
        ),
    ]
    if d.leaderboard:
        rows = [
            tag(
                "tr",
                tag("td", i, class_="num"),
                tag("td", row["name"]),
                tag("td", row["points"], class_="num"),
            )
            for i, row in enumerate(d.leaderboard, 1)
        ]
        head = tag("tr", tag("th", "#"), tag("th", "Name"), tag("th", "Points"))
        body.append(tag("table", tag("thead", head), tag("tbody", rows), class_="board"))
    else:
        body.append(tag("p", "No points yet.", class_="empty"))
    return page("Leaderboard", body)


def achievements_page(d: SiteData) -> bytes:
    numbers = {run["id"]: run["number"] for run in d.runs}
    runs_with: dict[str, list[int]] = {}
    for run_id, keys in d.unlocked.items():
        for key in keys:
            runs_with.setdefault(key, []).append(numbers.get(run_id, 0))
    rows: list[Markup] = []
    for a in d.achievements:
        reward = [f"+{a.points} points each"] if a.points else []
        if a.lives:
            reward.append(f"+{_lives(a.lives)}")
        found = sorted(runs_with.get(a.key, []))
        rows.append(
            tag(
                "tr",
                tag("td", tag("strong", a.name)),
                tag("td", a.description),
                tag("td", ", ".join(reward) or "—"),
                tag(
                    "td",
                    [f"{len(found)} {'run' if len(found) == 1 else 'runs'}, first ", _run_link(found[0])]
                    if found
                    else "not yet",
                ),
            )
        )
    head = tag("tr", [tag("th", h) for h in ("Achievement", "How", "Reward", "Unlocked")])
    return page(
        "Achievements",
        [
            tag("h1", "Achievements"),
            tag(
                "p",
                "Each can be unlocked once a run. Everyone who sent a command in the 10 minutes before gets "
                "its points; lives go to the run.",
            ),
            tag("table", tag("thead", head), tag("tbody", rows), class_="achievements")
            if rows
            else tag("p", "None yet.", class_="empty"),
        ],
    )


# key, what it means, unit ("%": a fraction shown as a percentage)
LAW_TEXT: tuple[tuple[str, str, str], ...] = (
    ("democracy_window_s", "In democracy, chat's most voted command runs every", "s"),
    ("meter_step", "Each !anarchy or !democracy moves the meter by", "points"),
    ("meter_democracy_at", "Democracy begins when the meter reaches", ""),
    ("meter_anarchy_at", "Anarchy comes back when the meter falls to", ""),
    ("meter_push_gap_s", "Each person can push the meter once every", "s"),
    ("meter_drift_s", "The meter drifts back to the middle one point every (0: never)", "s"),
    ("mode_lock_s", "After a switch, the mode stays at least", "s"),
    ("auto_snapshot_min", "The machine is snapshotted every", "min"),
    ("auto_snapshot_keep", "Automatic snapshots kept", ""),
    ("named_saves", "Named save slots", ""),
    ("lives_start", "Lives at the start of a run", ""),
    ("veto_min", "A passed vote is cancelled during its countdown by at least this many !veto", ""),
    ("veto_fraction", "…or by this share of its yes votes, if that's more", "%"),
)


def _number(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value * 100:.0f} %"
    text = f"{value:g}"
    return f"{text} {unit}" if unit else text


def laws_page(d: SiteData) -> bytes:
    laws, ruleset = d.laws, d.ruleset
    rows = [
        tag(
            "tr",
            tag("td", text),
            tag("td", _number(float(getattr(laws, key)), unit), class_="num"),
            tag("td", "amended" if key in d.amended else "", class_="note"),
        )
        for key, text, unit in LAW_TEXT
    ]
    votes: list[Markup] = []
    for action in ACTIONS:
        if action == "impeach" and "driver_seat" not in BUILT:
            continue
        if not ruleset.allows(action):
            continue
        t = laws.tier(action)
        votes.append(
            tag(
                "tr",
                tag("td", tag("code", f"!{action}")),
                tag("td", f"{t.threshold * 100:.0f} %", class_="num"),
                tag("td", f"{t.quorum_fraction * 100:.0f} %, at least {t.quorum_floor}"),
                tag("td", f"{t.window_s} s", class_="num"),
                tag("td", f"{t.veto_s} s" if t.veto_s else "—", class_="num"),
                tag("td", f"{t.cooldown_min:g} min", class_="num"),
            )
        )
    features = sorted(f for f in ruleset.features if f in BUILT)
    body: list[object] = [
        tag("h1", "The Laws"),
        tag(
            "p",
            "The numbers chat lives by, in the ruleset ",
            tag("strong", ruleset.name),
            ". Turned on: ",
            ", ".join(features) or "nothing",
            ".",
        ),
        tag("h2", "Modes, the meter, snapshots and lives"),
        tag("table", tag("tbody", rows), class_="laws"),
        tag("h2", "Power votes"),
        tag(
            "p",
            "Propose one with its command, then ",
            tag("code", "!yes"),
            " or ",
            tag("code", "!no"),
            ". It passes with this share of yes votes and enough of them (the quorum), then a countdown "
            "lets chat veto it. The quorum is a share of the people active in chat when the vote "
            "opened, with a minimum.",
        ),
        tag(
            "table",
            tag(
                "thead",
                tag("tr", [tag("th", h) for h in ("Vote", "Needs", "Quorum", "Voting", "Veto", "Cooldown")]),
            ),
            tag("tbody", votes),
            class_="votes",
        ),
        tag("h2", "Never up for a vote"),
        tag(
            "p",
            "The filters, the rate limits, the network's rules, bans, the panic button and the moderators' "
            "powers, and the rules of ",
            tag("code", "!newrun"),
            ".",
        ),
    ]
    disabled = sorted(ruleset.disabled)
    if disabled:
        body.insert(2, tag("p", "Turned off in this ruleset: ", ", ".join(f"!{c}" for c in disabled), "."))
    return page("The Laws", body)


def text_page(title: str, text: Markup) -> bytes:
    return page(title, tag("article", text, class_="text"))


def not_found() -> bytes:
    return page(
        "Not here",
        [tag("h1", "Not here"), tag("p", "No such page. ", tag("a", "Back home", href="/index.html"), ".")],
        up="/",  # served for any missing path, at any depth
    )


# --- writing it -------------------------------------------------------------------------------


def _write(target: Path, data: bytes) -> None:
    """Atomically: a page is never seen half written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    os.replace(temporary, target)


class Site:
    def __init__(self, out: Path, source: Path, thumbs: Path | None, url: str = "") -> None:
        self.out = out  # where brain writes it (SITE_DIR)
        self.source = source  # style.css and pages/*.md (SITE_SOURCE: the repo's site/)
        self.thumbs = thumbs  # snapshots' thumbnails and runs' last screens
        self.url = url.rstrip("/")  # its public address, for links in Discord ("" if none)
        self.generated: float | None = None
        self.files = 0
        self.warnings: list[str] = []
        self._hashes: dict[Path, tuple[int, int, str]] = {}  # image -> size, mtime, SHA-256

    def link(self, number: int) -> str:
        """A run's page, for #graveyard ("" without SITE_URL)."""
        return f"{self.url}/runs/{number}.html" if self.url else ""

    def _texts(self) -> dict[str, Markup]:
        texts: dict[str, Markup] = {}
        warnings: list[str] = []
        for name in PAGES:
            path = self.source / "pages" / f"{name}.md"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                warnings.append(f"{path} is missing")
                text = "This page isn't written yet."
            left = sorted(set(PLACEHOLDER.findall(_COMMENT.sub("", text))))
            if left:
                warnings.append(f"{name}.md still has {', '.join(left)}")
            texts[name] = markdown(text)
        self.warnings = warnings
        return texts

    def render(self, d: SiteData) -> tuple[dict[str, bytes], dict[str, Path]]:
        """Every page (path -> bytes) and the pictures they show (path -> file in thumbs/)."""
        texts = self._texts()
        try:
            css = (self.source / "style.css").read_bytes()
        except OSError:
            self.warnings.append(f"{self.source / 'style.css'} is missing")
            css = b""
        images = self._pictures(d)
        d.pictures = frozenset(path.name for path in images.values())  # pages show only these
        files: dict[str, bytes] = {
            "index.html": home(d, texts["about"]),
            "graveyard.html": graveyard(d),
            "leaderboard.html": leaderboard(d),
            "achievements.html": achievements_page(d),
            "laws.html": laws_page(d),
            "rules.html": text_page("Rules", texts["rules"]),
            "privacy.html": text_page("Privacy", texts["privacy"]),
            "404.html": not_found(),
            "style.css": css,
        }
        for run in d.runs:
            files[f"runs/{run['number']}.html"] = run_page(run, d)
        return files, images

    def _pictures(self, d: SiteData) -> dict[str, Path]:
        """The runs' pictures still in thumbs/ (a moderator may have taken some down)."""
        wanted: set[str] = set()
        for run in d.runs:
            if run["final_frame"]:
                wanted.add(run["final_frame"])
            wanted.update(s["thumb"] for s in d.thumbnails.get(run["id"], []))
        images: dict[str, Path] = {}
        if self.thumbs is not None:
            for name in sorted(wanted):
                path = self.thumbs / name
                if IMAGE.fullmatch(name) and path.is_file() and not path.is_symlink():
                    images[f"img/{name}"] = path
        return images

    def build(self, d: SiteData) -> int:
        """Render and publish; how many files changed. Runs in a thread (brain's render_site)."""
        files, images = self.render(d)
        changed = self.publish(files, images, d.now)
        self.generated, self.files = d.now, len(files) + len(images)
        return changed

    def _hash(self, path: Path) -> str:
        st = path.stat()
        known = self._hashes.get(path)
        if known and known[:2] == (st.st_size, st.st_mtime_ns):
            return known[2]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._hashes[path] = (st.st_size, st.st_mtime_ns, digest)
        return digest

    def publish(self, files: dict[str, bytes], images: dict[str, Path], now: float) -> int:
        out = self.out
        out.mkdir(parents=True, exist_ok=True)
        changed = 0
        manifest: dict[str, dict[str, Any]] = {}
        for rel, data in files.items():
            target = out / rel
            same = target.is_file() and not target.is_symlink() and target.stat().st_size == len(data)
            if not (same and target.read_bytes() == data):
                _write(target, data)
                changed += 1
            manifest[rel] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for rel, source in images.items():
            target = out / rel
            if not target.is_file() or target.is_symlink() or self._hash(target) != self._hash(source):
                _write(target, source.read_bytes())
                changed += 1
            manifest[rel] = {"size": target.stat().st_size, "sha256": self._hash(target)}
        keep = {*manifest, "manifest.json"}
        for path in sorted(out.rglob("*"), reverse=True):  # what's in a folder comes before it
            rel = path.relative_to(out).as_posix()
            if path.is_symlink() or (path.is_file() and rel not in keep):
                path.unlink()
                self._hashes.pop(path, None)
                changed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if changed or not (out / "manifest.json").is_file():
            body = {"version": 1, "generated": round(now), "files": manifest}
            _write(out / "manifest.json", json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        return changed

    def status_text(self) -> str:
        if self.generated is None:
            text = f"Site: not rendered yet ({self.out})."
        else:
            text = f"Site: {self.files} files in {self.out}, rendered {when(self.generated)}."
        if self.url:
            text += f" Public at {self.url}/"
        return "\n".join([text, *(f"⚠️ {w}" for w in self.warnings)])
