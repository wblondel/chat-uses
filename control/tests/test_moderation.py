from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.safety.moderation import (
    LOCKDOWN_AGE,
    Moderation,
    describe_duration,
    parse_duration,
)
from chatuses.safety.strikes import Strikes
from chatuses.store import PERMANENT, Store


@dataclass
class Effects:
    """Records what moderation asked brain to do."""

    done: list[str] = field(default_factory=lambda: [])

    def panic_changed(self, on: bool, kind: str = "panic") -> None:
        self.done.append(f"panic {on}" if kind == "panic" else f"{kind} {on}")

    def lockdown_changed(self, on: bool) -> None:
        self.done.append(f"lockdown {on}")

    def alert(self, text: str) -> None:
        self.done.append(f"alert {text}")

    def snapshot(self, name: str) -> None:
        self.done.append(f"snapshot {name}")


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as s:
        yield s


def moderation(store: Store) -> tuple[Moderation, Effects]:
    effects = Effects()
    return Moderation(
        store, effects, Strikes(), lambda user, name: "viewer#0001" if "bad" in name else name
    ), effects


def seen(store: Store, user: str, name: str, ts: float = 1000.0, roles: frozenset[str] = frozenset()) -> None:
    store.seen_user(user, name, ts, roles)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30),
        ("10m", 600),
        ("2h", 7200),
        ("7d", 604800),
        ("1w", 604800),
        ("1d12h", 129600),
        (" 5 M ", 300),
    ],
)
def test_durations(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "10", "m", "-5m", "1y", "0m", "10 minutes", "9999999d"])
def test_not_durations(text: str) -> None:
    assert parse_duration(text) is None


def test_forever_and_descriptions() -> None:
    assert parse_duration("perm") == parse_duration("Forever") == float("inf")
    assert describe_duration(float("inf")) == "permanently"
    assert describe_duration(600) == "for 10 minutes"
    assert describe_duration(86400) == "for 1 day"
    assert describe_duration(129600) == "for 36 hours"
    assert describe_duration(45) == "for 45 seconds"


def test_panic_covers_first_then_logs_snapshots_and_alerts(store: Store) -> None:
    mod, effects = moderation(store)
    answer = mod.panic("Mia (dc:1) on Discord", "something bad", now=1_700_000_000.0)
    assert answer.ok and mod.panicking
    assert effects.done[0] == "panic True"  # before anything else
    assert effects.done[1].startswith("alert 🚨 **Panic** by Mia (dc:1) on Discord: something bad.")
    assert effects.done[2] == "snapshot panic-20231114-221320"
    [event] = store.recent_events(("panic",))
    assert event[3] == {"by": "Mia (dc:1) on Discord", "reason": "something bad"}


def test_a_second_panic_covers_again_but_takes_no_new_snapshot(store: Store) -> None:
    mod, effects = moderation(store)
    mod.panic("a", now=100.0)
    effects.done.clear()
    answer = mod.panic("b", now=200.0)
    assert answer.ok and "already" in answer.text
    assert effects.done == ["panic True"]


def test_resume_and_a_panic_outlives_a_restart(store: Store) -> None:
    mod, _ = moderation(store)
    mod.panic("a", "why", now=100.0)
    again, effects = moderation(store)  # brain restarted
    assert again.panicking and again.panic_state == {"since": 100.0, "by": "a", "reason": "why", "kind": "panic"}
    answer = again.resume("b", now=400.0)
    assert answer.ok and not again.panicking
    assert effects.done[0] == "panic False" and "after 5 minutes" in effects.done[1]
    assert store.recent_events(("resume",))[0][3] == {"by": "b", "seconds": 300, "kind": "panic"}
    assert not moderation(store)[0].panicking
    assert not again.resume("c").ok  # nothing to end


def test_an_unreadable_panic_setting_stays_on(store: Store) -> None:
    store.set_setting("panic", "[not an object")
    assert moderation(store)[0].panicking


def test_ban_by_id_name_shown_name_and_mention(store: Store) -> None:
    mod, effects = moderation(store)
    seen(store, "yt:alice", "Alice")
    seen(store, "yt:troll", "a bad name")
    assert mod.ban("m", "Alice", "2h", "spam", now=1000.0).ok
    assert store.banned_until("yt:alice") == 1000.0 + 7200
    assert "Alice (yt:alice) banned for 2 hours by m: spam." in effects.done[-1]
    assert mod.ban("m", "VIEWER#0001", "perm", now=1000.0).ok  # as the overlay showed them
    assert store.banned_until("yt:troll") == PERMANENT
    assert mod.ban("m", "<@42>", "1d", now=1000.0).ok  # a Discord mention
    assert store.banned_until("dc:42") == 1000.0 + 86400
    assert mod.ban("m", "yt:never-seen", "1w", now=1000.0).ok  # by ID, before they show up
    [ban] = [e for e in store.recent_events(("ban",)) if e[3]["user"] == "yt:alice"]
    assert ban[3] == {"user": "yt:alice", "until": 8200.0, "by": "m", "reason": "spam"}


def test_bans_refused(store: Store) -> None:
    mod, _ = moderation(store)
    seen(store, "yt:mod", "Mod", roles=frozenset({"moderator"}))
    seen(store, "yt:a", "Sam")
    seen(store, "dc:b", "sam")
    assert "moderator" in mod.ban("m", "yt:mod", "1h", now=1000.0).text
    assert "Several people" in mod.ban("m", "Sam", "1h", now=1000.0).text
    assert "Nobody called" in mod.ban("m", "Nobody", "1h", now=1000.0).text
    assert "Duration" in mod.ban("m", "yt:a", "soon", now=1000.0).text
    assert not mod.ban("m", "", "1h").ok
    assert store.banned_until("yt:a") == 0


def test_names_are_only_looked_up_among_recent_people(store: Store) -> None:
    mod, _ = moderation(store)
    seen(store, "yt:old", "Oldtimer", ts=0.0)
    assert not mod.ban("m", "Oldtimer", "1h", now=8 * 86400.0).ok
    assert mod.ban("m", "yt:old", "1h", now=8 * 86400.0).ok


def test_a_moderator_can_shorten_a_ban_and_lift_it(store: Store) -> None:
    mod, effects = moderation(store)
    seen(store, "yt:a", "Alice")
    mod.ban("m", "yt:a", "7d", now=1000.0)
    mod.timeout("m", "yt:a", 5, now=1000.0)  # moderators set the end; the automatic timeouts only lengthen
    assert store.banned_until("yt:a") == 1300.0
    assert store.recent_events(("timeout",))[0][3]["by"] == "m"
    mod.strikes.add("yt:a", 1000.0)
    mod.strikes.add("yt:a", 1001.0)
    answer = mod.unban("m2", "Alice", now=1100.0)
    assert answer.ok and store.banned_until("yt:a") == 0
    assert mod.strikes.add("yt:a", 1102.0) is None  # their strikes were forgotten too
    assert "unbanned by m2" in effects.done[-1]
    assert not mod.unban("m2", "yt:a", now=1200.0).ok  # not banned any more


def test_timeout_limits(store: Store) -> None:
    mod, _ = moderation(store)
    assert not mod.timeout("m", "yt:a", 0).ok
    assert not mod.timeout("m", "yt:a", 7 * 24 * 60 + 1).ok
    assert mod.timeout("m", "yt:a", now=0.0).ok
    assert store.banned_until("yt:a") == 600.0


def test_lockdown_keeps_new_untrusted_people_out(store: Store) -> None:
    mod, effects = moderation(store)
    now = 100 * 86400.0
    seen(store, "yt:old", "Old", ts=now - LOCKDOWN_AGE - 1)
    seen(store, "yt:new", "New", ts=now - 60)

    def msg(user: str, roles: frozenset[str] = frozenset()) -> ChatMessage:
        return ChatMessage(user, "x", "!enter", now, roles)

    assert not mod.held_by_lockdown(msg("yt:new"))  # no lockdown yet
    assert mod.lockdown("m", True, now=now).ok
    assert effects.done[0] == "lockdown True" and "**Lockdown** on" in effects.done[1]
    assert mod.held_by_lockdown(msg("yt:new"))
    assert mod.held_by_lockdown(msg("yt:unknown"))
    assert not mod.held_by_lockdown(msg("yt:old"))
    for role in ("regular", "member", "moderator", "owner"):
        assert not mod.held_by_lockdown(msg("yt:new", frozenset({role})))
    assert not mod.lockdown("m", True).ok  # already on
    assert moderation(store)[0].locked  # kept across a restart
    assert mod.lockdown("m", False, now=now).ok
    assert not mod.held_by_lockdown(msg("yt:new"))
    assert [e[3]["on"] for e in store.recent_events(("lockdown",))] == [False, True]


def test_lockdown_and_panic_in_the_pipeline(store: Store) -> None:
    mod, _ = moderation(store)
    pipe = Pipeline(store, locked_out=mod.held_by_lockdown, paused=lambda: mod.panicking)
    mod.lockdown("m", True, now=0.0)
    assert pipe.handle(ChatMessage("yt:raider", "R", "!enter", 10.0)).kind == "locked"
    assert (
        pipe.handle(ChatMessage("dc:reg", "Reg", "!enter", 10.0, frozenset({"regular"}))).kind == "accepted"
    )
    mod.lockdown("m", False)
    mod.panic("m", now=20.0)
    assert pipe.handle(ChatMessage("yt:raider", "R", "!enter", 30.0)).kind == "paused"
    assert pipe.handle(ChatMessage("yt:raider", "R", "!help", 31.0)).kind == "accepted"  # not input
    assert store.outcome_counts() == {"locked": 1, "accepted": 2, "paused": 1}


def test_state_lists_bans(store: Store) -> None:
    mod, _ = moderation(store)
    seen(store, "yt:a", "Alice")
    mod.ban("m", "yt:a", "1h", now=0.0)
    mod.ban("m", "yt:b", "perm", now=0.0)
    state = mod.state(now=10.0)
    assert state["panic"] is None and state["lockdown"] is None
    assert state["banned"] == [
        {"user": "yt:b", "name": "", "until": None},
        {"user": "yt:a", "name": "Alice", "until": 3600.0},
    ]
    assert mod.state(now=4000.0)["banned"] == [{"user": "yt:b", "name": "", "until": None}]
