import json
import sqlite3
from pathlib import Path

import pytest

from chatuses.commands import Click
from chatuses.store import Store


def test_users_commands_and_events(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain.db")
    store.seen_user("yt:a", "Alice", 1.0)
    store.seen_user("yt:a", "Alice2", 2.0)
    name, first, last = store.db.execute("SELECT display_name, first_seen, last_seen FROM users").fetchone()
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
    store = Store(tmp_path / "brain.db")
    store.event(1.0, "test", {"n": 1})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("UPDATE events SET type = 'changed'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("DELETE FROM events")
    assert [e[2] for e in store.events()] == ["test"]


def test_reopening_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "brain.db"
    store = Store(path)
    store.event(1.0, "first", {})
    store.close()
    assert [e[2] for e in Store(path).events()] == ["first"]
