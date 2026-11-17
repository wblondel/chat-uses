"""The public site (Phase 8 step 3): pages from the store, escaped, published by changes."""

import asyncio
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from chatuses.brain import Brain, Config
from chatuses.commands import TypeText
from chatuses.executor import connect_to
from chatuses.game.achievements import parse
from chatuses.referee.laws import DEFAULT, Laws
from chatuses.safety.textfilter import TextFilter
from chatuses.site import (
    Markup,
    Site,
    SiteData,
    esc,
    gather,
    inline,
    markdown,
    page,
    tag,
)
from chatuses.store import Store

REPO = Path(__file__).resolve().parents[2]
T = 1_800_000_000.0
HOSTILE = '<img src=x onerror="alert(1)">'
PNG = b"\x89PNG\r\n\x1a\nfake"
ACHIEVEMENTS = parse(
    """
- {key: doom_guy, name: Doom Guy, description: Get Freedoom running,
   check: {type: process, pattern: doom}, reward: {points: 50, lives: 1}}
- {key: hello, name: Hello World, description: Say hello, check: {type: ocr_text, pattern: hello}}
"""
)


# --- HTML and Markdown --------------------------------------------------------------------


def test_text_is_escaped_unless_it_was_built_here() -> None:
    assert tag("p", "<b>&") == "<p>&lt;b&gt;&amp;</p>"
    assert tag("a", "x", href='"><script>') == '<a href="&quot;&gt;&lt;script&gt;">x</a>'
    assert tag("p", tag("b", "ok"), ["a", ["<", None, False]]) == "<p><b>ok</b>a&lt;</p>"
    assert tag("img", src="a.png", alt="", hidden=True, title=None) == '<img src="a.png" alt="" hidden>'
    assert tag("td", 3, class_="num") == '<td class="num">3</td>'
    assert esc(Markup("<b>")) == "<b>" and esc("<b>") == "&lt;b&gt;"


def test_markdown_keeps_its_few_forms_and_escapes_the_rest() -> None:
    html = markdown(
        """<!-- a note for whoever edits it -->
# Title

Some **bold** and `code <x>`, a [link](https://example.org/a?b=1) and <script>alert(1)</script>.

- one
  more of one
- [bad](javascript:alert(1)) [page](rules.html) [mail](mailto:a@b.c)

1. first
2. second

---
"""
    )
    assert "note" not in html
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html and "<code>code &lt;x&gt;</code>" in html
    assert '<a href="https://example.org/a?b=1">link</a>' in html
    assert "&lt;script&gt;" in html and "<script" not in html
    assert "<li>one more of one</li>" in html
    assert "[bad](javascript:alert(1))" in html and 'href="javascript' not in html
    assert '<a href="rules.html">page</a>' in html and '<a href="mailto:a@b.c">mail</a>' in html
    assert "<ol><li>first</li><li>second</li></ol>" in html and "<hr>" in html
    assert inline("[x](LINK TO THE SITE)") == "[x](LINK TO THE SITE)"


# --- pages from the store ----------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    """Run #1 died with a hostile name in it; run #2 is live."""
    with Store(":memory:") as s:
        for user, name in (("yt:a", "Alice"), ("dc:b", HOSTILE), ("yt:c", "Carol")):
            s.seen_user(user, name, T, frozenset())
        first = s.open_run(T, "roguelike", "chatpc", 3)
        for i, user in enumerate(("yt:a", "dc:b", "dc:b", "yt:c")):
            s.record_command(T + 10 + i, user, "!type x", TypeText(f"hi{i}"), "accepted", None)
        snap = s.add_snapshot(T + 20, "named", "base", "chatpc", "save-base-1", first["id"])
        s.set_snapshot_thumb(snap, f"{snap}.png")
        s.unlock(first["id"], "doom_guy", T + 30, ["dc:b", "yt:a"], 50)
        s.add_points(["yt:a"], 7)
        last = s.last_inputs(T, T + 60, 10)
        s.end_run(first["id"], T + 3600, "it froze", {**last[0], "name": "Carol"}, last, "run-1.png")
        second = s.open_run(T + 3700, "roguelike", "chatpc", 3)
        s.record_command(T + 3710, "yt:a", "!type ls", TypeText("ls"), "accepted", None)
        s.unlock(second["id"], "hello", T + 3720, ["yt:a"], 0)
        yield s


def data(store: Store, now: float = T + 7300) -> SiteData:
    return gather(
        store,
        achievements=ACHIEVEMENTS,
        ruleset=DEFAULT,
        laws=Laws(),
        safe_name=TextFilter().safe_name,
        lives_left=2,
        now=now,
    )


def site_in(tmp: Path) -> Site:
    thumbs = tmp / "thumbs"
    thumbs.mkdir()
    (thumbs / "1.png").write_bytes(PNG)
    (thumbs / "run-1.png").write_bytes(PNG + b"last")
    return Site(tmp / "out", REPO / "site", thumbs, "https://chat.example")


def test_the_pages_show_the_runs_and_never_raw_html_from_chat(store: Store, tmp_path: Path) -> None:
    site = site_in(tmp_path)
    files, images = site.render(data(store))
    assert set(files) >= {
        "index.html", "graveyard.html", "leaderboard.html", "achievements.html", "laws.html",
        "rules.html", "privacy.html", "404.html", "style.css", "runs/1.html", "runs/2.html",
    }  # fmt: skip
    assert set(images) == {"img/1.png", "img/run-1.png"}
    for path, body in files.items():
        if path.endswith(".html"):
            text = body.decode()
            assert "<script" not in text and 'onerror="' not in text, path
            assert text.startswith("<!doctype html>")
    dead = files["runs/1.html"].decode()
    assert "it froze" in dead and "Lived" in dead and "1 h 00 min" in dead
    assert '<img src="../img/run-1.png"' in dead  # its last screen
    assert "Probably killed by <strong>Carol</strong>: <code>!type hi3</code>" in dead
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in dead  # the name, as text
    assert "Doom Guy" in dead and "Thanks to " in dead
    assert "save “base”" in dead and '<img src="../img/1.png"' in dead  # the timeline
    assert dead.index("2 commands") < dead.index("1 command<")  # who played most, first
    live = files["runs/2.html"].decode()
    assert "Alive for" in live and "2 of 3" in live and "1 of 2" in live  # lives, achievements
    assert "<code>!type ls</code>" in live
    home = files["index.html"].decode()
    assert 'href="runs/2.html">Run #2</a>' in home and "Recent deaths" in home
    graveyard = files["graveyard.html"].decode()
    assert 'href="runs/1.html">Run #1</a>' in graveyard and "<td>Carol</td>" in graveyard
    board = files["leaderboard.html"].decode()
    assert board.index("Alice") < board.index("&lt;img")  # 57 points before 50
    achievements = files["achievements.html"].decode()
    assert "1 run, first " in achievements and "+50 points each, +1 life" in achievements
    assert files["404.html"].decode().count('href="/style.css"') == 1


def test_the_laws_page_says_the_numbers_and_what_was_amended(store: Store, tmp_path: Path) -> None:
    store.set_law(T, "democracy_window_s", 20, "console")
    laws = Laws(democracy_window_s=20)
    d = data(store)
    d.laws = laws
    page = Site(tmp_path, REPO / "site", None).render(d)[0]["laws.html"].decode()
    assert 'runs every</td><td class="num">20 s</td><td class="note">amended</td>' in page
    assert '<code>!newrun</code></td><td class="num">75 %</td>' in page
    assert '<code>!impeach</code></td><td class="num">50 %</td>' in page  # the driver's seat is built


def test_publishing_writes_what_changed_and_removes_the_rest(store: Store, tmp_path: Path) -> None:
    site = site_in(tmp_path)
    out = site.out
    d = data(store)
    assert site.build(d) > 10
    manifest = json.loads((out / "manifest.json").read_text())
    for rel, entry in manifest["files"].items():
        body = (out / rel).read_bytes()
        assert entry == {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}, rel
    assert (out / "img/run-1.png").read_bytes() == PNG + b"last"
    stamp = (out / "graveyard.html").stat().st_mtime_ns
    (out / "stray.html").write_text("x")
    (out / "runs" / "link.html").symlink_to("/etc/hostname")
    assert site.build(d) == 2  # the stray file and the link, nothing else
    assert not (out / "stray.html").exists() and not (out / "runs" / "link.html").is_symlink()
    assert (out / "graveyard.html").stat().st_mtime_ns == stamp
    later = data(store, T + 9000)
    assert site.build(later) == 2  # the home page and the live run's age
    assert site.status_text().startswith(f"Site: {site.files} files in {out}, rendered ")


def test_a_deletion_request_takes_the_name_off_every_page(store: Store, tmp_path: Path) -> None:
    site = site_in(tmp_path)
    store.forget(T + 8000, "dc:b")
    files, _images = site.render(data(store))
    for path, body in files.items():
        assert b"onerror" not in body, path
    dead = files["runs/1.html"].decode()
    assert "someone" in dead  # their last commands stay, without the name


def test_placeholders_and_missing_texts_are_reported(tmp_path: Path, store: Store) -> None:
    source = tmp_path / "src"
    (source / "pages").mkdir(parents=True)
    (source / "pages" / "about.md").write_text("<!-- [IGNORED NOTE] -->\nRun by [OPERATOR NAME].\n")
    site = Site(tmp_path / "out", source, None)
    files, _ = site.render(data(store))
    assert "about.md still has [OPERATOR NAME]" in site.warnings
    assert any("rules.md is missing" in w for w in site.warnings)
    assert any("style.css is missing" in w for w in site.warnings)
    assert b"written yet." in files["rules.html"]
    shipped = Site(tmp_path / "o2", REPO / "site", None)
    shipped.render(data(store))
    assert not any("privacy.md" in w for w in shipped.warnings)  # filled in: an audit reads it


def test_a_run_s_contributors_outlive_its_commands(store: Store) -> None:
    first = store.runs()[-1]
    assert first["contributors"] == [
        {"user": "dc:b", "commands": 2},
        {"user": "yt:a", "commands": 1},
        {"user": "yt:c", "commands": 1},
    ]
    store.db.execute("DELETE FROM commands")  # what the 90-day retention does to them
    assert store.runs()[-1]["contributors"] == first["contributors"]
    store.forget(T, "yt:a")
    assert [c["user"] for c in store.runs()[-1]["contributors"]] == ["dc:b", "yt:c"]


# --- through brain -------------------------------------------------------------------------


def test_brain_renders_it_when_something_changes(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            site = site_in(tmp_path)
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1), site=site, achievements=ACHIEVEMENTS)
            assert brain._site_dirty  # pyright: ignore[reportPrivateUsage]
            assert brain.overlay.site == "https://chat.example"  # the stream's notice links its rules
            status, text = await brain.admin(["site"])
            assert status == "ok" and "files changed just now" in text
            left = [w for w in site.warnings if "still has" in w]  # the shipped texts' placeholders
            assert all(w in text for w in left) and ("still has" in text) == bool(left)
            assert (site.out / "runs/1.html").is_file() and not brain._site_dirty  # pyright: ignore[reportPrivateUsage]
            brain.runs.give_life("test")
            assert brain._site_dirty  # pyright: ignore[reportPrivateUsage]
            assert await brain.render_site() == 2  # the home page and the run's
            assert site.link(4) == "https://chat.example/runs/4.html"
            assert Brain(store, lambda: connect_to("127.0.0.1", 1)).overlay.site == ""
            assert await Brain(store, lambda: connect_to("127.0.0.1", 1)).admin(["site"]) == (
                "error",
                "no public site in this brain",
            )

    asyncio.run(test())


def test_a_moderator_takes_a_run_s_pictures_off_the_site(store: Store, tmp_path: Path) -> None:
    async def test() -> None:
        site = site_in(tmp_path)
        brain = Brain(store, lambda: connect_to("127.0.0.1", 1), thumbs=site.thumbs, site=site)
        await brain.render_site()
        assert (site.out / "img/run-1.png").is_file() and (site.out / "img/1.png").is_file()
        status, text = await brain.admin(["site", "hide", "1"])
        assert status == "ok" and text.startswith("Run #1: 2 pictures deleted")
        await brain.render_site()
        assert not (site.out / "img").exists()
        assert b"img/" not in (site.out / "runs/1.html").read_bytes()
        assert await brain.admin(["site", "hide", "9"]) == ("error", "no run #9")

    asyncio.run(test())


def test_a_site_that_can_t_be_written_doesn_t_stop_brain(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            (tmp_path / "file").write_text("x")
            site = Site(tmp_path / "file" / "out", REPO / "site", None)
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1), site=site)
            assert await brain.render_site() is None
            status, _text = await brain.admin(["site"])
            assert status == "error"

    asyncio.run(test())


def test_the_site_s_address_is_checked() -> None:
    base = {"TARGET_HOST": "x"}
    assert Config.from_env({**base, "SITE_URL": "https://chat.example/"}).site_url == "https://chat.example"
    assert Config.from_env(base).site_url == ""
    for bad in ("chat.example", "https://chat.example/<x>", "javascript:alert(1)", "https://a b"):
        with pytest.raises(SystemExit):
            Config.from_env({**base, "SITE_URL": bad})


def test_the_shipped_pages_say_what_youtube_s_api_rules_ask() -> None:
    """YouTube API Services Developer Policies III.A (terms, privacy) and III.E.4 (30 days,
    deletion within 7 days): an audit reads these pages."""
    privacy = (REPO / "site/pages/privacy.md").read_text()
    rules = (REPO / "site/pages/rules.md").read_text()
    for text in (
        "YouTube API Services",
        "https://www.youtube.com/t/terms",
        "https://www.google.com/policies/privacy",
        "30 days",
        "within 7 days",
        "doesn't affect what YouTube or Discord store",
    ):
        assert text in privacy, text
    assert "https://www.youtube.com/t/terms" in rules and "agree to be bound" in rules
    footer = page("x", []).decode()
    assert 'href="https://www.youtube.com/t/terms"' in footer and 'href="privacy.html"' in footer


def test_the_privacy_notice_s_contact_is_left_for_cloudflare_to_hide(tmp_path: Path, store: Store) -> None:
    """Behind Cloudflare, its Email Address Obfuscation hides the address from spam robots and a
    script of its own shows it to people: the page gives it a plain mailto link to work on, and
    Caddy's policy lets that script (and only Cloudflare's path) run."""
    files, _ = Site(tmp_path / "out", REPO / "site", None).render(data(store))
    html = files["privacy.html"].decode()
    assert re.findall(r'<a href="mailto:[^"]+">', html) and "email_off" not in html
    caddy = (REPO / "infra/host/Caddyfile.tmpl").read_text()
    [policy] = re.findall(r'Content-Security-Policy "([^"]+)"', caddy)
    assert "default-src 'none'" in policy and "script-src https://${SITE_DOMAIN}/cdn-cgi/scripts/;" in policy
