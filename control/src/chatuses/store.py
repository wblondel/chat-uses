"""SQLite store (PLAN.md 5.8): users, commands, and the append-only events
log (debugging, replays, the public site later).

WAL mode, one connection, used from brain's single event loop. Events can't
be changed or deleted: triggers in the schema refuse it. Later phases add
their tables (votes, runs, snapshots...) here.
"""

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,              -- yt:<channel id>, dc:<user id>
    display_name TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    banned_until REAL,
    roles TEXT NOT NULL DEFAULT ''   -- space-separated, as the platform last said
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    raw TEXT NOT NULL,
    parsed_json TEXT,                 -- null when it didn't parse
    outcome TEXT NOT NULL,            -- accepted, invalid, rate_limited, filtered, banned
    reason TEXT
);
CREATE INDEX IF NOT EXISTS commands_ts ON commands(ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,             -- e.g. youtube_video
    value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def to_json(value: object) -> str:
    """JSON for a command dataclass (with its type) or plain data."""
    if is_dataclass(value) and not isinstance(value, type):
        return json.dumps({"type": type(value).__name__, **asdict(value)})
    return json.dumps(value)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit transactions
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database from an earlier version up to SCHEMA (columns added since)."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
        if "roles" not in columns:  # Phase 4 step 4
            self.db.execute("ALTER TABLE users ADD COLUMN roles TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def seen_user(
        self, user_id: str, display_name: str, ts: float, roles: frozenset[str] = frozenset()
    ) -> None:
        self.db.execute(
            """INSERT INTO users (id, display_name, first_seen, last_seen, roles) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name,
                                             last_seen = excluded.last_seen, roles = excluded.roles""",
            (user_id, display_name, ts, ts, " ".join(sorted(roles))),
        )

    def setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.db.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def record_command(
        self, ts: float, user_id: str, raw: str, parsed: object | None, outcome: str, reason: str | None
    ) -> None:
        """One chat command and what happened to it, plus its event, atomically."""
        parsed_json = to_json(parsed) if parsed is not None else None
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT INTO commands (ts, user_id, raw, parsed_json, outcome, reason)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, user_id, raw, parsed_json, outcome, reason),
            )
            self._event(ts, "command", {"user": user_id, "outcome": outcome, "command": parsed_json})

    def event(self, ts: float, type_: str, payload: dict[str, Any]) -> None:
        self._event(ts, type_, payload)

    def _event(self, ts: float, type_: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events (ts, type, payload_json) VALUES (?, ?, ?)", (ts, type_, json.dumps(payload))
        )

    def events(self, after_id: int = 0, limit: int = 1000) -> list[tuple[int, float, str, dict[str, Any]]]:
        rows = self.db.execute(
            "SELECT id, ts, type, payload_json FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return [(i, ts, t, json.loads(p)) for i, ts, t, p in rows]

    def outcome_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT outcome, COUNT(*) FROM commands GROUP BY outcome").fetchall())
