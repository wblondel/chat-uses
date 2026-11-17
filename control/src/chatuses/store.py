"""SQLite store (PLAN.md 5.8): users, commands, and the append-only events
log (debugging, replays, the audit viewer, the public site later).

WAL mode, one connection, used from brain's single event loop. Events can't
be changed, and can be deleted only once they're past the retention period
(90 days, PLAN.md 11) or for someone's deletion request (`forget`): triggers
in the schema refuse anything else. Votes and laws are here (referee/), and
chat's snapshots (snapshots.py); later phases add theirs (runs...).

What YouTube's API gave us (a viewer's channel ID, name and badges, and
their messages tied to them) is kept 30 days at most unless YouTube gave it
again since (YouTube API Services Developer Policies III.E.4: delete or
refresh after 30 days): `expire_youtube`, daily. A viewer YouTube hasn't
told us about for 30 days is forgotten, as for a deletion request; older
records of the others lose them. `refreshed` is when YouTube last gave us a
viewer's data: each message, and for a banned viewer, brain's check that
the channel still exists (channels.list).
"""

import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from chatuses.commands import describe_stored

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,              -- yt:<channel id>, dc:<user id>
    display_name TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    banned_until REAL,
    roles TEXT NOT NULL DEFAULT '',  -- space-separated, as the platform last said
    points INTEGER NOT NULL DEFAULT 0, -- PLAN.md 6.11 (game/points.py)
    refreshed REAL                    -- when the platform last gave us this user's data (YouTube: 30 days)
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
CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,               -- reboot, save, reset, rewind, load, newrun, impeach
    proposer TEXT NOT NULL,           -- '' once forgotten or past the retention period
    opened_at REAL NOT NULL,
    closed_at REAL,
    params_json TEXT NOT NULL,        -- {"text": "!rewind 2", "arg": 2}
    tally_json TEXT,                  -- yes, no, vetoes, quorum, active, result (words)
    eligible_count INTEGER NOT NULL,
    result TEXT                       -- done, error, failed, vetoed, cancelled; null while open
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,                   -- runs come with Phase 8
    kind TEXT NOT NULL,               -- auto, named
    name TEXT,                        -- a named save's name
    instance TEXT NOT NULL,
    backend_ref TEXT NOT NULL,        -- the Incus snapshot's name
    created_at REAL NOT NULL,
    thumb_path TEXT,                  -- thumbs/<id>.png, or null
    deleted_at REAL
);
CREATE TABLE IF NOT EXISTS runs (  -- Phase 8 (game/runs.py)
    id INTEGER PRIMARY KEY,
    number INTEGER NOT NULL UNIQUE,
    ruleset TEXT NOT NULL,
    instance TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,                    -- null while it runs
    lives_start INTEGER NOT NULL,
    lives_bonus INTEGER NOT NULL DEFAULT 0,  -- given back by achievements
    lives_used INTEGER NOT NULL DEFAULT 0,   -- rewinds, loads, revives
    cause TEXT,                       -- how it ended
    final_blow_json TEXT,             -- {"user", "name", "text"}: a joke, not an accusation
    last_commands_json TEXT,          -- [{"user", "name", "text", "ts"}], its last commands
    final_frame TEXT,                 -- thumbs/<file> of its last screen, or null
    contributors_json TEXT            -- [{"user", "commands"}]: who played most, kept with the run
);
CREATE TABLE IF NOT EXISTS achievements (  -- Phase 8 (game/achievements.py)
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    key TEXT NOT NULL,
    at REAL NOT NULL,
    credited_users_json TEXT NOT NULL,  -- who sent a command in the 10 minutes before
    UNIQUE (run_id, key)
);
CREATE TABLE IF NOT EXISTS macros (  -- Phase 9 (PLAN.md 6.10): chat's own commands, across runs
    name TEXT PRIMARY KEY,
    source TEXT NOT NULL,             -- its steps as chat writes them: "key ctrl+alt+t; type ls"
    proposed_by TEXT NOT NULL,        -- who proposed it ('' after a deletion request)
    added_at REAL NOT NULL,
    vote_id INTEGER REFERENCES votes(id)
);
CREATE TABLE IF NOT EXISTS amendments (  -- Phase 9 (PLAN.md 6.10): chat's changes to the laws
    id INTEGER PRIMARY KEY,           -- its number: Amendment #id
    name TEXT NOT NULL,               -- chat's name for the law: democracy_window
    key TEXT NOT NULL,                -- the laws table's: democracy_window_s, tiers.rewind.threshold
    value REAL NOT NULL,              -- the law's new value
    proposed_by TEXT NOT NULL,        -- who proposed it ('' after a deletion request)
    vote_id INTEGER REFERENCES votes(id),
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS laws (
    key TEXT PRIMARY KEY,             -- a law, or tiers.ACTION.SETTING (referee/laws.py)
    value TEXT NOT NULL,              -- JSON number, over the ruleset's
    amended_at REAL NOT NULL,
    amended_by TEXT NOT NULL,
    amendment_vote_id INTEGER REFERENCES votes(id)
);
CREATE TABLE IF NOT EXISTS sessions (  -- Phase 7: counts only, never who
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,                    -- null while it runs
    samples_json TEXT NOT NULL DEFAULT '[]',  -- [[ts, viewers or null, {"yt": n, "dc": n}], ...]
    report_json TEXT                  -- the numbers, once it ended (sessions.py)
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
YOUTUBE_KEEP = 30 * 86400.0  # seconds YouTube's API data is kept unless refreshed (III.E.4)
YOUTUBE_VIEWER = "a YouTube viewer"  # what's left of a YouTube viewer's name after that

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
        if "points" not in columns:  # Phase 8
            self.db.execute("ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
        if "refreshed" not in columns:  # YouTube's 30 days
            self.db.execute("ALTER TABLE users ADD COLUMN refreshed REAL")
            self.db.execute("UPDATE users SET refreshed = last_seen")
        if "contributors_json" not in {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN contributors_json TEXT")  # Phase 8 step 3
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
            """INSERT INTO users (id, display_name, first_seen, last_seen, roles, refreshed)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name,
                                             last_seen = excluded.last_seen, roles = excluded.roles,
                                             refreshed = excluded.refreshed""",
            (user_id, display_name, ts, ts, " ".join(sorted(roles)), ts),
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
                """INSERT INTO users (id, display_name, first_seen, last_seen, banned_until, refreshed)
                   VALUES (?, '', ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET banned_until = excluded.banned_until""",
                (user_id, ts, ts, until, ts),
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

    def event(self, ts: float, type_: str, payload: dict[str, Any]) -> int:
        """Log it; its ID."""
        return self._event(ts, type_, payload)

    def _event(self, ts: float, type_: str, payload: dict[str, Any]) -> int:
        cursor = self.db.execute(
            "INSERT INTO events (ts, type, payload_json) VALUES (?, ?, ?)", (ts, type_, json.dumps(payload))
        )
        return int(cursor.lastrowid or 0)

    # --- screen OCR verdicts (Phase 7) ----------------------------------------------------

    def ocr_hit(self, hit_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT ts, payload_json FROM events WHERE id = ? AND type = 'ocr_hit'", (hit_id,)
        ).fetchone()
        return {"id": hit_id, "ts": row[0], **json.loads(row[1])} if row else None

    def ocr_verdicts(self, hit_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
        """Each hit's latest verdict ({"verdict": "real" or "false", "by", "ts"}), of these hits or all."""
        rows = self.db.execute(
            "SELECT ts, payload_json FROM events WHERE type = 'ocr_verdict' ORDER BY id"
        ).fetchall()
        wanted = set(hit_ids) if hit_ids is not None else None
        verdicts: dict[int, dict[str, Any]] = {}
        for ts, payload in rows:
            data = json.loads(payload)
            hit = int(data.get("hit", 0))
            if wanted is None or hit in wanted:
                verdicts[hit] = {"verdict": data.get("verdict"), "by": data.get("by"), "ts": ts}
        return verdicts

    def ocr_summary(self) -> dict[str, Any]:
        """Every hit still in the log (90 days), by mode and verdict: what decides enforcing."""
        hits = [
            (i, json.loads(p).get("mode", "?"))
            for i, p in self.db.execute("SELECT id, payload_json FROM events WHERE type = 'ocr_hit'")
        ]
        verdicts = self.ocr_verdicts()
        kinds = Counter(verdicts[i]["verdict"] if i in verdicts else "unmarked" for i, _mode in hits)
        return {
            "hits": len(hits),
            "modes": dict(sorted(Counter(mode for _i, mode in hits).items())),
            "real": kinds["real"],
            "false": kinds["false"],
            "unmarked": kinds["unmarked"],
        }

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

    # --- votes and laws (referee/) -------------------------------------------------------

    def open_vote(
        self, kind: str, proposer: str, opened: float, params: dict[str, Any], eligible: int
    ) -> int:
        with self.db:
            self.db.execute("BEGIN")
            cursor = self.db.execute(
                "INSERT INTO votes (kind, proposer, opened_at, params_json, eligible_count)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, proposer, opened, json.dumps(params), eligible),
            )
            vote_id = int(cursor.lastrowid or 0)
            self._event(opened, "vote", {"id": vote_id, "kind": kind, "user": proposer, **params})
        return vote_id

    def close_vote(self, vote_id: int, closed: float, tally: dict[str, Any], result: str) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "UPDATE votes SET closed_at = ?, tally_json = ?, result = ? WHERE id = ?",
                (closed, json.dumps(tally), result, vote_id),
            )
            self._event(closed, "vote-result", {"id": vote_id, "phase": result, **tally})

    def votes(self, limit: int = 50) -> list[dict[str, Any]]:
        """The latest votes, newest first."""
        rows = self.db.execute(
            "SELECT id, kind, proposer, opened_at, closed_at, params_json, tally_json, eligible_count, result"
            " FROM votes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": i,
                "kind": kind,
                "proposer": proposer,
                "opened": opened,
                "closed": closed,
                "params": json.loads(params),
                "tally": json.loads(tally) if tally else None,
                "eligible": eligible,
                "result": result,
            }
            for i, kind, proposer, opened, closed, params, tally, eligible, result in rows
        ]

    def add_snapshot(
        self, ts: float, kind: str, name: str | None, instance: str, ref: str, run_id: int | None = None
    ) -> int:
        with self.db:
            self.db.execute("BEGIN")
            cursor = self.db.execute(
                "INSERT INTO snapshots (run_id, kind, name, instance, backend_ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, kind, name, instance, ref, ts),
            )
            snapshot_id = int(cursor.lastrowid or 0)
            self._event(ts, "snapshot-saved", {"id": snapshot_id, "kind": kind, "name": name, "ref": ref})
        return snapshot_id

    def set_snapshot_thumb(self, snapshot_id: int, path: str) -> None:
        self.db.execute("UPDATE snapshots SET thumb_path = ? WHERE id = ?", (path, snapshot_id))

    def delete_snapshot(self, ts: float, snapshot_id: int) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("UPDATE snapshots SET deleted_at = ? WHERE id = ?", (ts, snapshot_id))
            self._event(ts, "snapshot-deleted", {"id": snapshot_id})

    def snapshots(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Snapshots still there, newest first."""
        where, args = "deleted_at IS NULL", list[Any]()
        if kind is not None:
            where += " AND kind = ?"
            args.append(kind)
        rows = self.db.execute(
            "SELECT id, kind, name, instance, backend_ref, created_at, thumb_path FROM snapshots"
            f" WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        keys = ("id", "kind", "name", "instance", "ref", "created", "thumb")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def laws(self) -> dict[str, float]:
        """Laws changed while running, over the ruleset's."""
        return {
            key: float(json.loads(value)) for key, value in self.db.execute("SELECT key, value FROM laws")
        }

    def set_law(self, ts: float, key: str, value: float | None, by: str) -> None:
        """Change a law (None: back to the ruleset's), logged. Bounds are the caller's (referee/laws.py)."""
        with self.db:
            self.db.execute("BEGIN")
            if value is None:
                self.db.execute("DELETE FROM laws WHERE key = ?", (key,))
            else:
                self.db.execute(
                    """INSERT INTO laws (key, value, amended_at, amended_by) VALUES (?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                           amended_at = excluded.amended_at, amended_by = excluded.amended_by""",
                    (key, json.dumps(value), ts, by),
                )
            self._event(ts, "law", {"law": key, "value": value, "by": by})

    def prune(self, now: float) -> tuple[int, int]:
        """Delete commands and events past the retention period; how many of each.

        Votes stay, without who proposed them."""
        cutoff = now - RETENTION
        with self.db:
            self.db.execute("BEGIN")
            commands = self.db.execute("DELETE FROM commands WHERE ts < ?", (cutoff,)).rowcount
            events = self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            self.db.execute(
                "UPDATE votes SET proposer = '' WHERE opened_at < ? AND proposer != ''", (cutoff,)
            )
        return commands, events

    def forget(self, ts: float, user_id: str) -> tuple[int, int]:
        """A deletion request: the user's commands and the events naming them.

        A ban survives (the row stays, nameless) so it can't be shed this way;
        otherwise the user goes too. Logged, without the ID."""
        with self.db:
            self.db.execute("BEGIN")
            commands, events = self._forget(ts, user_id)
            self._event(ts, "forget", {"commands": commands, "events": events})
        return commands, events

    def _forget(
        self, ts: float, user_id: str, keep_ban: bool = True, name: str = "someone"
    ) -> tuple[int, int]:
        """forget's work, in the caller's transaction (name: what the Graveyard says instead)."""
        commands = self.db.execute("DELETE FROM commands WHERE user_id = ?", (user_id,)).rowcount
        events = self._erase_events(
            "SELECT id FROM events WHERE json_extract(payload_json, '$.user') = ?", (user_id,)
        )
        self.db.execute("UPDATE votes SET proposer = '' WHERE proposer = ?", (user_id,))
        self.db.execute("UPDATE macros SET proposed_by = '' WHERE proposed_by = ?", (user_id,))
        self.db.execute("UPDATE amendments SET proposed_by = '' WHERE proposed_by = ?", (user_id,))
        self._scrub_runs(lambda user: user == user_id, name, "1", ())
        self._scrub_credits(
            lambda user: user == user_id, "instr(credited_users_json, ?)", (json.dumps(user_id),)
        )
        if keep_ban and self.banned_until(user_id) > ts:
            self.db.execute("UPDATE users SET display_name = '', roles = '' WHERE id = ?", (user_id,))
        else:
            self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return commands, events

    def _erase_events(self, select: str, params: tuple[Any, ...]) -> int:
        """Delete the events the query names (the append-only trigger lets each through)."""
        ids = [row[0] for row in self.db.execute(select, params).fetchall()]
        for event_id in ids:
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('erasing', ?)", (event_id,))
            self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.db.execute("DELETE FROM settings WHERE key = 'erasing'")
        return len(ids)

    def _scrub_credits(self, match: Callable[[str], bool], where: str, params: tuple[Any, ...]) -> None:
        """Take users out of the achievements they were credited with."""
        for achievement_id, credited in self.db.execute(
            f"SELECT id, credited_users_json FROM achievements WHERE {where}", params
        ).fetchall():
            kept = [u for u in json.loads(credited) if not match(u)]
            self.db.execute(
                "UPDATE achievements SET credited_users_json = ? WHERE id = ?",
                (json.dumps(kept), achievement_id),
            )

    def backup(self, path: Path, now: float, keep: float) -> dict[str, int]:
        """A copy of the store at `path` for the host's backups (infra/host/backup.py), made
        with SQLite's online backup while brain runs. YouTube's data in it is gone as it will
        be `keep` seconds from now (expire_youtube), so a backup kept that long never holds any
        past its 30 days; compacted, so what was deleted is gone from the file too. Returns
        what expire_youtube took out."""
        part = path.with_name(path.name + ".part")
        part.unlink(missing_ok=True)
        target = sqlite3.connect(part)
        try:
            self.db.backup(target)
        finally:
            target.close()
        with Store(part) as copy:
            expired = copy.expire_youtube(now + keep)
            copy.db.execute("PRAGMA journal_mode=DELETE")  # one file, no -wal beside it
            copy.db.execute("VACUUM")
        os.replace(part, path)
        return expired

    def expire_youtube(self, now: float) -> dict[str, int]:
        """YouTube's API data, 30 days on (YOUTUBE_KEEP): viewers YouTube hasn't told us about
        since are forgotten (a ban in force keeps a nameless row: brain refreshes those); older
        records of the others lose them. What chat built (runs, macros, laws) stays, nameless.
        How many of each; logged without IDs."""
        cutoff = now - YOUTUBE_KEEP
        with self.db:
            self.db.execute("BEGIN")
            stale = [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM users WHERE id LIKE 'yt:%' AND coalesce(refreshed, last_seen) < ?",
                    (cutoff,),
                )
            ]
            forgotten = [self._forget(now, user_id, name=YOUTUBE_VIEWER) for user_id in stale]
            commands = (
                sum(c for c, _e in forgotten)
                + self.db.execute(
                    "DELETE FROM commands WHERE user_id LIKE 'yt:%' AND ts < ?", (cutoff,)
                ).rowcount
            )
            events = sum(e for _c, e in forgotten) + self._erase_events(
                "SELECT id FROM events WHERE ts < ? AND instr(payload_json, '\"yt:')", (cutoff,)
            )
            self.db.execute(
                "UPDATE votes SET proposer = '' WHERE proposer LIKE 'yt:%' AND opened_at < ?", (cutoff,)
            )
            self.db.execute(
                "UPDATE macros SET proposed_by = '' WHERE proposed_by LIKE 'yt:%' AND added_at < ?", (cutoff,)
            )
            self.db.execute(
                "UPDATE amendments SET proposed_by = '' WHERE proposed_by LIKE 'yt:%' AND at < ?", (cutoff,)
            )
            self.db.execute(
                "UPDATE laws SET amended_by = '' WHERE amended_by LIKE 'yt:%' AND amended_at < ?", (cutoff,)
            )
            youtube: Callable[[str], bool] = lambda user: user.startswith("yt:")  # noqa: E731
            self._scrub_runs(youtube, YOUTUBE_VIEWER, "ended_at < ?", (cutoff,))
            self._scrub_credits(youtube, "at < ? AND instr(credited_users_json, '\"yt:')", (cutoff,))
            self._scrub_session_viewers(cutoff)
            counts = {"viewers": len(stale), "commands": commands, "events": events}
            if any(counts.values()):
                self._event(now, "youtube-expiry", counts)
        return counts

    def _scrub_session_viewers(self, cutoff: float) -> None:
        """YouTube's concurrent viewers, in sessions older than the cutoff (a YouTube statistic:
        30 days too). brain no longer asks for them; older sessions may hold some."""
        for session_id, samples, report in self.db.execute(
            "SELECT id, samples_json, report_json FROM sessions WHERE started_at < ?", (cutoff,)
        ).fetchall():
            kept = [[s[0], None, *s[2:]] for s in json.loads(samples)]
            data = json.loads(report) if report else None
            if data is not None and "viewers" in data:
                data["viewers"] = {"peak": None, "average": None}
            self.db.execute(
                "UPDATE sessions SET samples_json = ?, report_json = ? WHERE id = ?",
                (json.dumps(kept), json.dumps(data) if data is not None else None, session_id),
            )

    def youtube_bans(self, now: float, older_than: float) -> list[str]:
        """Banned YouTube viewers whose data YouTube last gave us before `older_than`: brain asks
        YouTube whether their channels still exist, which refreshes them (YouTube's 30 days)."""
        return [
            row[0]
            for row in self.db.execute(
                "SELECT id FROM users WHERE id LIKE 'yt:%' AND banned_until > ?"
                " AND coalesce(refreshed, last_seen) < ? ORDER BY refreshed",
                (now, older_than),
            )
        ]

    def refreshed(self, user_ids: list[str], ts: float) -> None:
        """YouTube gave us these users' data again (they're still there)."""
        self.db.executemany("UPDATE users SET refreshed = ? WHERE id = ?", [(ts, u) for u in user_ids])

    def drop_users(self, ts: float, user_ids: list[str]) -> None:
        """Channels that are gone: forgotten, bans and all."""
        with self.db:
            self.db.execute("BEGIN")
            for user_id in user_ids:
                self._forget(ts, user_id, keep_ban=False, name=YOUTUBE_VIEWER)
            if user_ids:
                self._event(ts, "youtube-gone", {"viewers": len(user_ids)})

    # --- sessions (sessions.py) ------------------------------------------------------------

    def open_session(self, name: str, ts: float) -> int:
        with self.db:
            self.db.execute("BEGIN")
            cursor = self.db.execute("INSERT INTO sessions (name, started_at) VALUES (?, ?)", (name, ts))
            session_id = int(cursor.lastrowid or 0)
            self._event(ts, "session", {"id": session_id, "name": name, "phase": "start"})
        return session_id

    def running_session(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id, name, started_at, samples_json FROM sessions WHERE ended_at IS NULL ORDER BY id DESC"
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "started": row[2], "samples": json.loads(row[3])}

    def add_session_sample(self, session_id: int, sample: list[Any]) -> None:
        self.db.execute(
            "UPDATE sessions SET samples_json = json_insert(samples_json, '$[#]', json(?)) WHERE id = ?",
            (json.dumps(sample), session_id),
        )

    def close_session(self, session_id: int, ts: float, report: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "UPDATE sessions SET ended_at = ?, report_json = ? WHERE id = ?",
                (ts, json.dumps(report), session_id),
            )
            self._event(ts, "session", {"id": session_id, "phase": "end"})

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """The latest sessions, newest first, with their reports (None while running)."""
        rows = self.db.execute(
            "SELECT id, name, started_at, ended_at, report_json FROM sessions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ("id", "name", "started", "ended", "report")
        return [dict(zip(keys, (*r[:4], json.loads(r[4]) if r[4] else None), strict=True)) for r in rows]

    def session_numbers(self, start: float, end: float) -> dict[str, Any]:
        """What happened between start and end: commands, votes and the events a report counts."""
        window = (start, end)
        outcomes = dict(
            self.db.execute(
                "SELECT outcome, COUNT(*) FROM commands WHERE ts >= ? AND ts < ? GROUP BY outcome", window
            ).fetchall()
        )
        people = dict(
            self.db.execute(
                "SELECT substr(user_id, 1, instr(user_id, ':') - 1), COUNT(DISTINCT user_id)"
                " FROM commands WHERE ts >= ? AND ts < ? GROUP BY 1",
                window,
            ).fetchall()
        )
        votes = self.db.execute(
            "SELECT kind, result FROM votes WHERE opened_at >= ? AND opened_at < ?", window
        ).fetchall()
        events = self.db.execute(
            "SELECT id, type, payload_json FROM events WHERE ts >= ? AND ts < ?"
            " AND type IN ('panic', 'resume', 'hold', 'ocr_hit', 'time-travel', 'power')",
            window,
        ).fetchall()
        hits = [i for i, kind, _payload in events if kind == "ocr_hit"]
        verdicts = self.ocr_verdicts(hits)
        return {
            "outcomes": outcomes,
            "people": people,
            "votes": [(kind, result) for kind, result in votes],
            "events": [(kind, json.loads(payload)) for _i, kind, payload in events],
            "ocr_verdicts": Counter(v["verdict"] for v in verdicts.values()),
        }

    # --- runs (game/runs.py) ------------------------------------------------------------

    _RUN = (
        "id, number, ruleset, instance, started_at, ended_at, lives_start, lives_bonus, lives_used,"
        " cause, final_blow_json, last_commands_json, final_frame, contributors_json"
    )

    def _run(self, row: tuple[Any, ...] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        keys = (
            "id", "number", "ruleset", "instance", "started", "ended", "lives_start", "lives_bonus",
            "lives_used", "cause", "final_blow", "last_commands", "final_frame", "contributors",
        )  # fmt: skip
        run: dict[str, Any] = dict(zip(keys, row, strict=True))
        run["final_blow"] = json.loads(run["final_blow"]) if run["final_blow"] else None
        run["last_commands"] = json.loads(run["last_commands"]) if run["last_commands"] else []
        run["contributors"] = json.loads(run["contributors"]) if run["contributors"] else None
        return run

    def open_run(self, ts: float, ruleset: str, instance: str, lives: int) -> dict[str, Any]:
        with self.db:
            self.db.execute("BEGIN")
            (number,) = self.db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM runs").fetchone()
            cursor = self.db.execute(
                "INSERT INTO runs (number, ruleset, instance, started_at, lives_start)"
                " VALUES (?, ?, ?, ?, ?)",
                (number, ruleset, instance, ts, lives),
            )
            self._event(ts, "run", {"number": number, "phase": "start", "lives": lives})
        run = self.run(int(cursor.lastrowid or 0))
        assert run is not None
        return run

    def run(self, run_id: int) -> dict[str, Any] | None:
        return self._run(self.db.execute(f"SELECT {self._RUN} FROM runs WHERE id = ?", (run_id,)).fetchone())

    def current_run(self) -> dict[str, Any] | None:
        return self._run(
            self.db.execute(
                f"SELECT {self._RUN} FROM runs WHERE ended_at IS NULL ORDER BY number DESC"
            ).fetchone()
        )

    def runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """The latest runs, newest first."""
        rows = self.db.execute(f"SELECT {self._RUN} FROM runs ORDER BY number DESC LIMIT ?", (limit,))
        return [run for row in rows if (run := self._run(row)) is not None]

    def run_lives(self, run_id: int, ts: float, used: int, bonus: int, why: str) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "UPDATE runs SET lives_used = ?, lives_bonus = ? WHERE id = ?", (used, bonus, run_id)
            )
            self._event(ts, "lives", {"run": run_id, "used": used, "bonus": bonus, "why": why})

    def end_run(
        self,
        run_id: int,
        ts: float,
        cause: str,
        final_blow: dict[str, Any] | None,
        last_commands: list[dict[str, Any]],
        final_frame: str | None,
    ) -> None:
        with self.db:
            self.db.execute("BEGIN")
            (started,) = self.db.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
            contributors = self.contributors(started, ts)  # kept: commands go after 90 days
            self.db.execute(
                "UPDATE runs SET ended_at = ?, cause = ?, final_blow_json = ?, last_commands_json = ?,"
                " final_frame = ?, contributors_json = ? WHERE id = ?",
                (ts, cause, json.dumps(final_blow) if final_blow else None, json.dumps(last_commands),
                 final_frame, json.dumps(contributors), run_id),
            )  # fmt: skip
            self._event(ts, "run", {"run": run_id, "phase": "end", "cause": cause})

    def contributors(self, start: float, end: float, limit: int = 20) -> list[dict[str, Any]]:
        """Who had the most commands accepted between start and end: [{"user", "commands"}]."""
        rows = self.db.execute(
            "SELECT user_id, COUNT(*) AS n FROM commands WHERE ts >= ? AND ts <= ? AND outcome = 'accepted'"
            " GROUP BY user_id ORDER BY n DESC, user_id LIMIT ?",
            (start, end, limit),
        )
        return [{"user": user, "commands": n} for user, n in rows]

    def last_inputs(self, start: float, end: float, limit: int = 10) -> list[dict[str, Any]]:
        """The last input commands accepted between start and end (not votes or meta commands),
        newest first, with who sent them."""
        rows = self.db.execute(
            "SELECT c.ts, c.user_id, u.display_name, c.parsed_json FROM commands c"
            " JOIN users u ON u.id = c.user_id WHERE c.ts >= ? AND c.ts <= ? AND c.outcome = 'accepted'"
            " AND json_extract(c.parsed_json, '$.type') IN"
            " ('TypeText', 'PressKeys', 'Click', 'Move', 'Drag', 'Scroll')"
            " ORDER BY c.ts DESC LIMIT ?",
            (start, end, limit),
        ).fetchall()
        return [
            {"ts": ts, "user": user, "name": name, "text": describe_stored(json.loads(parsed))}
            for ts, user, name, parsed in rows
        ]

    def _scrub_runs(
        self, match: Callable[[str], bool], name: str, where: str, params: tuple[Any, ...]
    ) -> None:
        """Dead runs keep their last commands and final blow (the Graveyard): take names out
        (the commands stay, as chat's), and the matching users out of their contributors."""

        def scrub(entry: dict[str, Any]) -> dict[str, Any]:
            return {**entry, "user": "", "name": name} if match(str(entry.get("user", ""))) else entry

        rows = self.db.execute(
            "SELECT id, final_blow_json, last_commands_json, contributors_json FROM runs WHERE " + where,
            params,
        ).fetchall()
        for run_id, blow, last, contributors in rows:
            blow_data = scrub(json.loads(blow)) if blow else None
            last_data = [scrub(e) for e in json.loads(last)] if last else []
            kept = [c for c in json.loads(contributors) if not match(c["user"])] if contributors else None
            self.db.execute(
                "UPDATE runs SET final_blow_json = ?, last_commands_json = ?, contributors_json = ?"
                " WHERE id = ?",
                (json.dumps(blow_data) if blow_data else None, json.dumps(last_data),
                 json.dumps(kept) if kept is not None else None, run_id),
            )  # fmt: skip

    # --- amendments (PLAN.md 6.10) ------------------------------------------------------------

    def add_amendment(
        self, ts: float, name: str, key: str, value: float, by: str, vote_id: int | None
    ) -> int:
        """Keep a passed amendment; its number. (The law itself is set_law's.)"""
        with self.db:
            self.db.execute("BEGIN")
            cursor = self.db.execute(
                "INSERT INTO amendments (name, key, value, proposed_by, vote_id, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, key, value, by, vote_id, ts),
            )
            number = int(cursor.lastrowid or 0)
            self._event(ts, "amendment", {"number": number, "law": key, "value": value})
        return number

    def amendments(self) -> list[dict[str, Any]]:
        """Every amendment, first first: {"number", "name", "key", "value", "by", "at"}."""
        rows = self.db.execute("SELECT id, name, key, value, proposed_by, at FROM amendments ORDER BY id")
        keys = ("number", "name", "key", "value", "by", "at")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # --- macros (PLAN.md 6.10) ---------------------------------------------------------------

    def macros(self) -> list[dict[str, Any]]:
        """Chat's macros, oldest first: {"name", "source", "by", "at"}."""
        rows = self.db.execute(
            "SELECT name, source, proposed_by, added_at FROM macros ORDER BY added_at, name"
        )
        return [{"name": n, "source": src, "by": by, "at": at} for n, src, by, at in rows]

    def add_macro(self, ts: float, name: str, source: str, by: str, vote_id: int | None = None) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT INTO macros (name, source, proposed_by, added_at, vote_id) VALUES (?, ?, ?, ?, ?)",
                (name, source, by, ts, vote_id),
            )
            self._event(ts, "macro", {"name": name, "source": source, "phase": "added"})

    def remove_macro(self, ts: float, name: str, by: str) -> bool:
        with self.db:
            self.db.execute("BEGIN")
            gone = self.db.execute("DELETE FROM macros WHERE name = ?", (name,)).rowcount
            if gone:
                self._event(ts, "macro", {"name": name, "phase": "removed", "by": by})
        return bool(gone)

    # --- achievements and points (game/) ---------------------------------------------------

    def unlocked(self, run_id: int) -> dict[str, dict[str, Any]]:
        """A run's achievements: key -> {"at", "credited"}."""
        rows = self.db.execute(
            "SELECT key, at, credited_users_json FROM achievements WHERE run_id = ? ORDER BY at", (run_id,)
        ).fetchall()
        return {key: {"at": at, "credited": json.loads(credited)} for key, at, credited in rows}

    def all_unlocked(self) -> dict[int, dict[str, dict[str, Any]]]:
        """Every run's achievements: run id -> key -> {"at", "credited"} (the public site)."""
        found: dict[int, dict[str, dict[str, Any]]] = {}
        for run_id, key, at, credited in self.db.execute(
            "SELECT run_id, key, at, credited_users_json FROM achievements ORDER BY at"
        ):
            found.setdefault(run_id, {})[key] = {"at": at, "credited": json.loads(credited)}
        return found

    def run_thumbnails(self) -> dict[int, list[dict[str, Any]]]:
        """Every run's snapshots that had a thumbnail, deleted ones too (their thumbnails stay
        for the site), oldest first: run id -> [{"id", "kind", "name", "created", "thumb"}]."""
        found: dict[int, list[dict[str, Any]]] = {}
        for snap_id, run_id, kind, name, created, thumb in self.db.execute(
            "SELECT id, run_id, kind, name, created_at, thumb_path FROM snapshots"
            " WHERE run_id IS NOT NULL AND thumb_path IS NOT NULL ORDER BY created_at, id"
        ):
            found.setdefault(run_id, []).append(
                {"id": snap_id, "kind": kind, "name": name, "created": created, "thumb": thumb}
            )
        return found

    def user_names(self, user_ids: list[str]) -> dict[str, str]:
        """Display names of those still known (a deletion request takes the name)."""
        names: dict[str, str] = {}
        ids = sorted(set(user_ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            names.update(
                self.db.execute(
                    f"SELECT id, display_name FROM users WHERE display_name != '' AND id IN"
                    f" ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
            )
        return names

    def unlock(self, run_id: int, key: str, ts: float, credited: list[str], points: int) -> bool:
        """Record it and give each credited user its points; False if the run had it already."""
        with self.db:
            self.db.execute("BEGIN")
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO achievements (run_id, key, at, credited_users_json)"
                " VALUES (?, ?, ?, ?)",
                (run_id, key, ts, json.dumps(credited)),
            )
            if cursor.rowcount == 0:
                return False
            if points:
                self.db.executemany(
                    "UPDATE users SET points = points + ? WHERE id = ?", [(points, user) for user in credited]
                )
            self._event(ts, "achievement", {"run": run_id, "key": key, "credited": len(credited)})
        return True

    def last_event_time(self, type_: str, since: float) -> float | None:
        (ts,) = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE type = ? AND ts >= ?", (type_, since)
        ).fetchone()
        return float(ts) if ts is not None else None

    def command_counts(self, since: float) -> dict[str, int]:
        """Accepted commands since then, by user (the driver's seat: who played this session)."""
        rows = self.db.execute(
            "SELECT user_id, COUNT(*) FROM commands WHERE ts >= ? AND outcome = 'accepted' GROUP BY user_id",
            (since,),
        )
        return {user: n for user, n in rows}

    def commanders(self, start: float, end: float) -> list[str]:
        """Who had a command accepted between start and end (an achievement's credit)."""
        rows = self.db.execute(
            "SELECT DISTINCT user_id FROM commands WHERE ts >= ? AND ts <= ? AND outcome = 'accepted'"
            " ORDER BY user_id",
            (start, end),
        )
        return [user for (user,) in rows]

    def spend_points(self, user_id: str, points: int) -> bool:
        """Take points (the chaos shop) if they have that many; whether they did."""
        cursor = self.db.execute(
            "UPDATE users SET points = points - ? WHERE id = ? AND points >= ?", (points, user_id, points)
        )
        return cursor.rowcount == 1

    def add_points(self, user_ids: list[str], points: int) -> None:
        if user_ids and points:
            self.db.executemany(
                "UPDATE users SET points = points + ? WHERE id = ?", [(points, user) for user in user_ids]
            )

    def leaderboard(self, limit: int = 100, now: float | None = None) -> list[dict[str, Any]]:
        """Users by points, most first: none with 0, nor anyone banned or timed out now."""
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT id, display_name, points FROM users WHERE points > 0 AND display_name != ''"
            " AND coalesce(banned_until, 0) <= ? ORDER BY points DESC, id LIMIT ?",
            (now, limit),
        ).fetchall()
        return [{"user": u, "name": n, "points": p} for u, n, p in rows]

    def standing(self, user_id: str) -> tuple[int, int, int]:
        """A user's points, their rank (1: first; 0: no points) and how many have points."""
        row = self.db.execute("SELECT points FROM users WHERE id = ?", (user_id,)).fetchone()
        points = int(row[0]) if row else 0
        (total,) = self.db.execute("SELECT COUNT(*) FROM users WHERE points > 0").fetchone()
        if points <= 0:
            return 0, 0, int(total)
        (ahead,) = self.db.execute("SELECT COUNT(*) FROM users WHERE points > ?", (points,)).fetchone()
        return points, int(ahead) + 1, int(total)

    def outcome_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT outcome, COUNT(*) FROM commands GROUP BY outcome").fetchall())
