"""SQLite store (PLAN.md 5.8): users, commands, and the append-only events
log (debugging, replays, the audit viewer, the public site later).

WAL mode, one connection, used from brain's single event loop. Events can't
be changed, and can be deleted only once they're past the retention period
(90 days, PLAN.md 11) or for someone's deletion request (`forget`): triggers
in the schema refuse anything else. Later phases add their tables (votes,
runs, snapshots...) here.
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
    outcome TEXT NOT NULL,            -- accepted, invalid, rate_limited, filtered, banned, locked, paused
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
    WHEN old.ts > unixepoch() - 7776000
     AND NOT EXISTS (SELECT 1 FROM settings WHERE key = 'erasing' AND value = old.id)
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


PERMANENT = 253402300799.0  # 9999-12-31: when a ban without an end ends
RETENTION = 90 * 86400.0  # seconds raw logs are kept (PLAN.md 11; the trigger says 7776000)

User = dict[str, Any]  # id, display_name, first_seen, last_seen, banned_until (0 if none), roles


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
        (trigger,) = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'events_no_delete'"
        ).fetchone()
        if "WHEN" not in trigger:  # Phase 5: retention and deletion requests
            self.db.execute("DROP TRIGGER events_no_delete")
            self.db.executescript(SCHEMA)

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

    def banned_until(self, user_id: str) -> float:
        """When the user's ban or timeout ends (0 if none)."""
        row = self.db.execute("SELECT banned_until FROM users WHERE id = ?", (user_id,)).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def time_out(self, ts: float, user_id: str, until: float, reason: str) -> None:
        """Keep the user from sending commands until `until` (never shortens a longer one), logged."""
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "UPDATE users SET banned_until = max(coalesce(banned_until, 0), ?) WHERE id = ?",
                (until, user_id),
            )
            self._event(ts, "timeout", {"user": user_id, "until": until, "reason": reason})

    def ban(self, ts: float, user_id: str, until: float, by: str, reason: str, kind: str = "ban") -> None:
        """A moderator's ban or timeout: ends at `until` exactly (it may shorten one), logged.

        The user needn't have been seen: a ban by ID can come first."""
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                """INSERT INTO users (id, display_name, first_seen, last_seen, banned_until)
                   VALUES (?, '', ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET banned_until = excluded.banned_until""",
                (user_id, ts, ts, until),
            )
            self._event(ts, kind, {"user": user_id, "until": until, "by": by, "reason": reason})

    def unban(self, ts: float, user_id: str, by: str) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE users SET banned_until = NULL WHERE id = ?", (user_id,))
            self._event(ts, "unban", {"user": user_id, "by": by})

    def user(self, user_id: str) -> User | None:
        rows = self._users("WHERE id = ?", (user_id,))
        return rows[0] if rows else None

    def users_seen_since(self, since: float, limit: int = 10_000) -> list[User]:
        """Users who sent a command since then, the most recent first."""
        return self._users("WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?", (since, limit))

    def banned(self, now: float, limit: int = 500) -> list[User]:
        """Bans and timeouts still running, the ones ending last first."""
        return self._users("WHERE banned_until > ? ORDER BY banned_until DESC LIMIT ?", (now, limit))

    def _users(self, where: str, args: tuple[Any, ...]) -> list[User]:
        rows = self.db.execute(
            "SELECT id, display_name, first_seen, last_seen, coalesce(banned_until, 0), roles FROM users "
            + where,
            args,
        ).fetchall()
        keys = ("id", "display_name", "first_seen", "last_seen", "banned_until", "roles")
        return [dict(zip(keys, row, strict=True)) for row in rows]

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

    def recent_commands(
        self, before_id: int | None = None, user_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """The audit viewer: commands, newest first, with who sent them."""
        where, args = ["1"], list[Any]()
        if before_id is not None:
            where.append("c.id < ?")
            args.append(before_id)
        if user_id is not None:
            where.append("c.user_id = ?")
            args.append(user_id)
        rows = self.db.execute(
            "SELECT c.id, c.ts, c.user_id, u.display_name, c.raw, c.outcome, c.reason"
            " FROM commands c JOIN users u ON u.id = c.user_id"
            f" WHERE {' AND '.join(where)} ORDER BY c.id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        keys = ("id", "ts", "user", "display_name", "raw", "outcome", "reason")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def recent_events(
        self, types: tuple[str, ...] = (), before_id: int | None = None, limit: int = 100
    ) -> list[tuple[int, float, str, dict[str, Any]]]:
        """Events of these types (all if none), newest first."""
        where, args = ["1"], list[Any]()
        if types:
            where.append(f"type IN ({', '.join('?' * len(types))})")
            args.extend(types)
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        rows = self.db.execute(
            f"SELECT id, ts, type, payload_json FROM events WHERE {' AND '.join(where)}"
            " ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [(i, ts, t, json.loads(p)) for i, ts, t, p in rows]

    def prune(self, now: float) -> tuple[int, int]:
        """Delete commands and events past the retention period; how many of each."""
        cutoff = now - RETENTION
        with self.db:
            self.db.execute("BEGIN")
            commands = self.db.execute("DELETE FROM commands WHERE ts < ?", (cutoff,)).rowcount
            events = self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        return commands, events

    def forget(self, ts: float, user_id: str) -> tuple[int, int]:
        """A deletion request: the user's commands and the events naming them.

        A ban survives (the row stays, nameless) so it can't be shed this way;
        otherwise the user goes too. Logged, without the ID."""
        with self.db:
            self.db.execute("BEGIN")
            commands = self.db.execute("DELETE FROM commands WHERE user_id = ?", (user_id,)).rowcount
            ids = [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM events WHERE json_extract(payload_json, '$.user') = ?", (user_id,)
                )
            ]
            for event_id in ids:
                self.db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('erasing', ?)", (event_id,)
                )
                self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self.db.execute("DELETE FROM settings WHERE key = 'erasing'")
            if self.banned_until(user_id) > ts:
                self.db.execute("UPDATE users SET display_name = '', roles = '' WHERE id = ?", (user_id,))
            else:
                self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._event(ts, "forget", {"commands": commands, "events": len(ids)})
        return commands, len(ids)

    def outcome_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT outcome, COUNT(*) FROM commands GROUP BY outcome").fetchall())
