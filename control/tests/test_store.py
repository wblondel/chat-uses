import json
import sqlite3
import time
from pathlib import Path

import pytest

from chatuses.commands import Click
from chatuses.store import Store


def test_users_commands_and_events(tmp_path: Path) -> None:
    with Store(tmp_path / "brain.db") as store:
        store.seen_user("yt:a", "Alice", 1.0)
        store.seen_user("yt:a", "Alice2", 2.0)
        name, first, last = store.db.execute(
            "SELECT display_name, first_seen, last_seen FROM users"
        ).fetchone()
        assert (name, first, last) == ("Alice2", 1.0, 2.0)

        store.record_command(2.0, "yt:a", "!click F7", Click("F7", 550, 650), "accepted", None)
        raw, parsed = store.db.execute("SELECT raw, parsed_json FROM commands").fetchone()
        assert raw == "!click F7"
        assert json.loads(parsed) == {
            "type": "Click",
            "cell": "F7",
            "x": 550,
            "y": 650,
            "button": "left",
            "double": False,
        }
        [(_, ts, kind, payload)] = store.events()
        assert (ts, kind, payload["outcome"], payload["user"]) == (2.0, "command", "accepted", "yt:a")
        assert store.outcome_counts() == {"accepted": 1}
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_events_are_append_only(tmp_path: Path) -> None:
    with Store(tmp_path / "brain.db") as store:
        store.event(time.time(), "test", {"n": 1})
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.db.execute("UPDATE events SET type = 'changed'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.db.execute("DELETE FROM events")
        assert [e[2] for e in store.events()] == ["test"]


def test_reopening_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "brain.db"
    with Store(path) as store:
        store.event(1.0, "first", {})
    with Store(path) as store:
        assert [e[2] for e in store.events()] == ["first"]


def test_a_database_from_step_3_gets_the_roles_column(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, first_seen REAL NOT NULL,
                                 last_seen REAL NOT NULL, banned_until REAL);
           INSERT INTO users VALUES ('yt:a', 'Alice', 1.0, 1.0, NULL);"""
    )
    old.close()
    with Store(path) as store:
        store.seen_user("yt:a", "Alice", 2.0, frozenset({"moderator", "member"}))
        assert store.db.execute("SELECT roles, first_seen FROM users").fetchone() == ("member moderator", 1.0)


def test_settings(tmp_path: Path) -> None:
    with Store(tmp_path / "brain.db") as store:
        assert store.setting("youtube_video") is None
        store.set_setting("youtube_video", "abc")
        store.set_setting("youtube_video", "def")
        assert store.setting("youtube_video") == "def"
        store.set_setting("youtube_video", None)
        assert store.setting("youtube_video") is None


def test_moderator_bans_set_the_end_and_unbans_clear_it() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "Alice", 1.0)
        store.time_out(2.0, "yt:a", 1000.0, "strikes")
        store.ban(3.0, "yt:a", 500.0, "mod", "shorter")  # a moderator may shorten it
        assert store.banned_until("yt:a") == 500.0
        store.ban(3.0, "dc:new", 900.0, "mod", "", kind="timeout")  # someone not seen yet
        assert store.user("dc:new") == {
            "id": "dc:new",
            "display_name": "",
            "first_seen": 3.0,
            "last_seen": 3.0,
            "banned_until": 900.0,
            "roles": "",
        }
        assert [u["id"] for u in store.banned(400.0)] == ["dc:new", "yt:a"]
        store.unban(4.0, "yt:a", "mod2")
        assert store.banned_until("yt:a") == 0.0
        assert [u["id"] for u in store.banned(400.0)] == ["dc:new"]
        types = [(t, p.get("by")) for _, _, t, p in store.recent_events()]
        assert types == [("unban", "mod2"), ("timeout", "mod"), ("ban", "mod"), ("timeout", None)]


def test_audit_queries_newest_first_with_paging_and_filters() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "Alice", 1.0)
        store.seen_user("dc:b", "Bob", 1.0)
        for i in range(5):
            store.record_command(float(i), "yt:a" if i % 2 else "dc:b", f"!type {i}", None, "invalid", "why")
        rows = store.recent_commands(limit=2)
        assert [r["raw"] for r in rows] == ["!type 4", "!type 3"]
        assert rows[1] == {
            "id": 4,
            "ts": 3.0,
            "user": "yt:a",
            "display_name": "Alice",
            "raw": "!type 3",
            "outcome": "invalid",
            "reason": "why",
        }
        assert [r["raw"] for r in store.recent_commands(before_id=rows[1]["id"])] == [
            "!type 2",
            "!type 1",
            "!type 0",
        ]
        assert [r["raw"] for r in store.recent_commands(user_id="yt:a")] == ["!type 3", "!type 1"]
        store.event(9.0, "panic", {"by": "m"})
        assert [t for _, _, t, _ in store.recent_events(("panic", "ban"))] == ["panic"]
        newest = store.recent_events(limit=1)[0][0]
        assert len(store.recent_events(before_id=newest)) == 5


def test_users_seen_since_most_recent_first() -> None:
    with Store(":memory:") as store:
        store.seen_user("yt:a", "Alice", 1.0)
        store.seen_user("yt:b", "Bob", 5.0)
        store.seen_user("yt:c", "Cy", 3.0)
        assert [u["id"] for u in store.users_seen_since(2.0)] == ["yt:b", "yt:c"]


def test_old_logs_are_pruned_and_only_old_events_can_be_deleted() -> None:
    now = time.time()
    with Store(":memory:") as store:
        store.seen_user("yt:a", "Alice", now)
        for age_days in (100, 91, 10):
            store.record_command(now - age_days * 86400, "yt:a", "!enter", None, "accepted", None)
        assert store.prune(now) == (2, 2)  # commands and their events past 90 days
        assert [c["ts"] for c in store.recent_commands()] == [now - 10 * 86400]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.db.execute("DELETE FROM events")  # the recent one stays protected
        assert store.user("yt:a") is not None  # people stay (points, leaderboards later)


def test_a_deletion_request_forgets_someone(tmp_path: Path) -> None:
    now = time.time()
    with Store(tmp_path / "brain.db") as store:
        store.seen_user("yt:a", "Alice", now)
        store.seen_user("yt:b", "Bob", now)
        store.record_command(now, "yt:a", "!type hi", None, "accepted", None)
        store.record_command(now, "yt:b", "!enter", None, "accepted", None)
        store.time_out(now, "yt:a", now - 1, "strikes")  # over already
        assert store.forget(now, "yt:a") == (1, 2)
        assert store.user("yt:a") is None
        assert [c["user"] for c in store.recent_commands()] == ["yt:b"]
        assert all(p.get("user") != "yt:a" for _, _, _, p in store.events())
        [log] = store.recent_events(("forget",))
        assert log[3] == {"commands": 1, "events": 2}  # without the ID
        assert store.setting("erasing") is None
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.db.execute("DELETE FROM events")  # protected again


def test_a_banned_user_stays_banned_when_forgotten() -> None:
    now = time.time()
    with Store(":memory:") as store:
        store.seen_user("yt:a", "Alice", now, frozenset({"member"}))
        store.ban(now, "yt:a", now + 3600, "mod", "spam")
        store.forget(now, "yt:a")
        user = store.user("yt:a")
        assert user is not None and user["display_name"] == "" and user["roles"] == ""
        assert store.banned_until("yt:a") == now + 3600


def test_a_database_from_step_3_gets_the_new_trigger(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript(
        """CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, type TEXT NOT NULL,
                                payload_json TEXT NOT NULL);
           CREATE TRIGGER events_no_delete BEFORE DELETE ON events
               BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
           INSERT INTO events (ts, type, payload_json) VALUES (1.0, 'old', '{}');"""
    )
    db.close()
    with Store(tmp_path / "old.db") as store:
        assert store.prune(time.time()) == (0, 1)
