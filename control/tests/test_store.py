import json
import sqlite3
import time
from pathlib import Path

import pytest

from chatuses.commands import Click
from chatuses.store import PERMANENT, Store


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


DAY = 86400.0


def test_youtube_data_goes_after_30_days_unless_youtube_gave_it_again() -> None:
    now = time.time()
    old = now - 40 * DAY
    with Store(":memory:") as store:
        for user, name in (("yt:gone", "Gone"), ("yt:back", "Back"), ("dc:friend", "Friend")):
            store.seen_user(user, name, old)
            store.record_command(old, user, "!key alt+f4", None, "accepted", None)
        store.seen_user("yt:back", "Back", now - DAY)  # came back yesterday: YouTube gave it again
        store.record_command(now - DAY, "yt:back", "!enter", None, "accepted", None)
        store.add_points(["yt:gone", "yt:back", "dc:friend"], 50)
        run = store.open_run(old - DAY, "roguelike", "chatpc", 3)
        blow = {"user": "yt:gone", "name": "Gone", "text": "!key alt+f4"}
        last = [blow, {"user": "dc:friend", "name": "Friend", "text": "!key alt+f4"}]
        store.end_run(run["id"], old, "it froze", blow, last, None)
        store.unlock(
            store.open_run(old, "roguelike", "chatpc", 3)["id"], "hello", old, ["yt:back", "dc:friend"], 10
        )
        vote = store.open_vote("save", "yt:back", old, {"text": "!save x"}, 3)
        store.add_macro(old, "term", "key ctrl+alt+t", "yt:back", vote)
        store.add_amendment(old, "democracy_window", "democracy_window_s", 20, "yt:back", vote)

        counts = store.expire_youtube(now)

        assert counts == {"viewers": 1, "commands": 2, "events": counts["events"]} and counts["events"] >= 3
        assert store.user("yt:gone") is None  # with its points
        back = store.user("yt:back")
        assert back is not None and back["display_name"] == "Back" and store.standing("yt:back")[0] == 60
        assert store.user("dc:friend") is not None and store.standing("dc:friend")[0] == 60
        assert [(c["user"], c["raw"]) for c in store.recent_commands()] == [
            ("yt:back", "!enter"),  # yesterday's stays
            ("dc:friend", "!key alt+f4"),  # Discord's: 90 days, as before
        ]
        for _id, ts, _type, payload in store.events():
            assert ts >= now - 30 * DAY or "yt:" not in json.dumps(payload)
        dead = store.run(run["id"])
        assert dead is not None
        assert dead["final_blow"] == {"user": "", "name": "a YouTube viewer", "text": "!key alt+f4"}
        assert [e["name"] for e in dead["last_commands"]] == ["a YouTube viewer", "Friend"]
        assert all(not c["user"].startswith("yt:") for c in dead["contributors"])
        assert ["yt:back" in json.dumps(a) for a in store.unlocked(run["id"] + 1).values()] == [False]
        assert store.macros()[0]["by"] == "" and store.amendments()[0]["by"] == ""
        assert store.db.execute("SELECT proposer FROM votes").fetchone() == ("",)
        [log] = store.recent_events(("youtube-expiry",))
        assert log[3] == counts and "yt:" not in json.dumps(log[3])
        assert store.expire_youtube(now) == {"viewers": 0, "commands": 0, "events": 0}  # nothing left to do
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.db.execute("DELETE FROM events")  # protected again


def test_a_backup_copy_holds_no_youtube_data_past_30_days_while_it_is_kept(tmp_path: Path) -> None:
    now = time.time()
    with Store(tmp_path / "brain.db") as store:
        store.seen_user("yt:old", "Old", now - 25 * DAY)  # 30 days old in 5: gone from a 7-day backup
        store.record_command(now - 25 * DAY, "yt:old", "!enter", None, "accepted", None)
        store.seen_user("yt:new", "New", now - DAY)
        store.seen_user("dc:friend", "Friend", now - 25 * DAY)  # Discord's: 90 days, as before
        store.set_setting("ruleset", "roguelike")
        target = tmp_path / "backup" / "brain.db"
        target.parent.mkdir()
        expired = store.backup(target, now, 7 * DAY)
        assert expired["viewers"] == 1 and expired["commands"] == 1
        assert store.user("yt:old") is not None  # the store itself keeps it: still within 30 days
        assert sorted(target.parent.iterdir()) == [target]  # one file: no -wal, no .part
        assert b"Old" not in target.read_bytes()  # compacted: not even in free pages
        with Store(target) as copy:
            assert copy.user("yt:old") is None and copy.user("yt:new") is not None
            assert copy.user("dc:friend") is not None and copy.setting("ruleset") == "roguelike"
        store.seen_user("dc:later", "Later", now)
        store.backup(target, now, 7 * DAY)  # again: replaced whole
        with Store(target) as copy:
            assert copy.user("dc:later") is not None


def test_a_banned_youtube_viewer_stays_banned_while_youtube_confirms_the_channel() -> None:
    now = time.time()
    with Store(":memory:") as store:
        store.seen_user("yt:troll", "Troll", now - 25 * DAY)
        store.ban(now - 25 * DAY, "yt:troll", PERMANENT, "mod", "spam")
        store.seen_user("yt:fine", "Fine", now - 25 * DAY)
        assert store.youtube_bans(now, older_than=now - 20 * DAY) == ["yt:troll"]  # due a check
        store.refreshed(["yt:troll"], now)  # the channel's still there
        assert store.youtube_bans(now, older_than=now - 20 * DAY) == []
        store.expire_youtube(now + 10 * DAY)
        assert store.banned_until("yt:troll") == PERMANENT
        assert store.user("yt:fine") is None
        store.drop_users(now, ["yt:troll"])  # the channel's gone: so is the ban
        assert store.user("yt:troll") is None
        # Never confirmed again (YouTube down for a month): nameless, the ban kept.
        store.seen_user("yt:other", "Other", now)
        store.ban(now, "yt:other", PERMANENT, "mod", "spam")
        store.expire_youtube(now + 31 * DAY)
        other = store.user("yt:other")
        assert (
            other is not None and other["display_name"] == "" and store.banned_until("yt:other") == PERMANENT
        )


def test_youtube_s_viewer_counts_go_from_sessions_after_30_days() -> None:
    now = time.time()
    with Store(":memory:") as store:
        old = store.open_session("beta", now - 40 * DAY)
        store.add_session_sample(old, [now - 40 * DAY, 42, {"yt": 3}])
        store.close_session(
            old, now - 40 * DAY + 3600, {"viewers": {"peak": 42, "average": 42.0}, "minutes": 60}
        )
        recent = store.open_session("later", now - DAY)
        store.add_session_sample(recent, [now - DAY, 7, {"yt": 1}])
        store.expire_youtube(now)
        samples = {
            row[0]: json.loads(row[1]) for row in store.db.execute("SELECT id, samples_json FROM sessions")
        }
        assert samples[old] == [[now - 40 * DAY, None, {"yt": 3}]]  # chat's own count stays
        assert samples[recent] == [[now - DAY, 7, {"yt": 1}]]
        (report,) = store.db.execute("SELECT report_json FROM sessions WHERE id = ?", (old,)).fetchone()
        assert json.loads(report) == {"viewers": {"peak": None, "average": None}, "minutes": 60}


def test_an_older_database_gets_the_refreshed_column(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript(
        """CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, first_seen REAL NOT NULL,
                               last_seen REAL NOT NULL, banned_until REAL, roles TEXT NOT NULL DEFAULT '',
                               points INTEGER NOT NULL DEFAULT 0);
           INSERT INTO users (id, display_name, first_seen, last_seen) VALUES ('yt:a', 'A', 1.0, 5.0);"""
    )
    db.close()
    with Store(tmp_path / "old.db") as store:
        assert store.db.execute("SELECT refreshed FROM users").fetchone() == (5.0,)


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
