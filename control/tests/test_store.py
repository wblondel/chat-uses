import json
import sqlite3
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
        store.event(1.0, "test", {"n": 1})
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
