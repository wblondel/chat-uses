from chatuses.commands import Click, Meta
from chatuses.game.runs import DEATH_AFTER, DEATH_VOTE, GRACE, Runs, uptime_text
from chatuses.identity import Presence
from chatuses.referee.laws import Laws
from chatuses.referee.votes import NOT_ELIGIBLE
from chatuses.store import Store

T = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = T

    def __call__(self) -> float:
        return self.now


def runs(store: Store, presence: Presence | None = None, lives: bool = True) -> tuple[Runs, Clock]:
    clock = Clock()
    return Runs(store, Laws, presence or Presence(), lambda: lives, clock=clock), clock


def settled(*users: str) -> Presence:
    presence = Presence()
    for user in users:
        presence.saw(user, T - 600)
        presence.saw(user, T + GRACE + 5)
    return presence


def die(r: Runs, clock: Clock, cause: str = "it froze") -> None:
    """Past the grace, ill long enough."""
    clock.now += GRACE + 10
    assert r.observe(cause, clock.now, quiet=False) is None
    clock.now += DEATH_AFTER
    assert r.observe(cause, clock.now, quiet=False) is not None


def test_the_first_run_and_its_lives() -> None:
    with Store(":memory:") as store:
        r, _clock = runs(store)
        run = r.ensure("roguelike", "chatpc")
        assert (run["number"], run["lives_start"], r.lives_left()) == (1, 3, 3)
        assert r.ensure("roguelike", "chatpc")["id"] == run["id"]  # a restart keeps it
        assert r.spend_life("rewind") and r.spend_life("load") and r.spend_life("rewind")
        assert r.lives_left() == 0 and not r.spend_life("rewind")
        r.give_life("achievement: Doom Guy")
        assert r.lives_left() == 1
        assert [p["why"] for *_, kind, p in store.events() if kind == "lives"] == [
            "rewind", "load", "rewind", "achievement: Doom Guy"
        ]  # fmt: skip


def test_lives_off_cost_nothing() -> None:
    with Store(":memory:") as store:
        r, _clock = runs(store, lives=False)
        r.ensure("living-pc", "chatpc")
        assert r.lives_left() is None and r.spend_life("rewind")
        assert store.current_run()["lives_used"] == 0  # type: ignore[index]


def test_a_death_takes_a_minute_of_illness_and_not_while_quiet_or_booting() -> None:
    with Store(":memory:") as store:
        r, clock = runs(store)
        r.ensure("roguelike", "chatpc")
        r.start("roguelike", "chatpc")  # a new run: GRACE to boot
        assert r.observe("it froze", T + 10, quiet=False) is None and r.state == "alive"
        clock.now = T + GRACE + 1
        assert r.observe("it froze", clock.now, quiet=False) is None
        assert r.observe("it froze", clock.now + DEATH_AFTER - 1, quiet=False) is None
        assert r.observe("it froze", clock.now + 30, quiet=True) is None  # a panic: the count starts again
        assert r.observe("it froze", clock.now + 40, quiet=False) is None
        assert r.observe(None, clock.now + 50, quiet=False) is None  # better: again
        assert r.observe("it froze", clock.now + 60, quiet=False) is None
        death = r.observe("it froze", clock.now + 60 + DEATH_AFTER, quiet=False)
        assert death is not None and death.cause == "it froze" and death.since == clock.now + 50
        assert r.state == "dying" and r.observe("it froze", clock.now + 999, quiet=False) is None


def test_the_death_vote_revive_wins_ties_and_silence() -> None:
    with Store(":memory:") as store:
        r, clock = runs(store, settled("a", "b", "c"))
        r.ensure("roguelike", "chatpc")
        die(r, clock)
        assert r.ballot("a", True, clock.now).ok and r.ballot("b", False, clock.now).ok
        assert r.ballot("raider", True, clock.now).text == NOT_ELIGIBLE
        view = r.view(clock.now)
        assert view is not None and view["death"] == {
            "cause": "it froze",
            "ends_in": 60.0,
            "revive": 1,
            "rip": 1,
        }
        assert r.verdict(clock.now + DEATH_VOTE - 1) is None
        assert r.verdict(clock.now + DEATH_VOTE) == "revive" and r.state == "ending"
        r.revived()
        assert r.state == "alive" and r.death is None
        die(r, clock)
        assert r.verdict(clock.now + DEATH_VOTE) == "revive"  # nobody voted


def test_rip_wins_with_more_votes_and_no_lives_means_no_vote() -> None:
    with Store(":memory:") as store:
        r, clock = runs(store, settled("a", "b", "c"))
        r.ensure("roguelike", "chatpc")
        die(r, clock)
        r.ballot("a", False, clock.now)
        r.ballot("b", False, clock.now)
        r.ballot("c", True, clock.now)
        r.ballot("c", False, clock.now)  # changed their mind
        assert r.verdict(clock.now + DEATH_VOTE) == "rip"
    with Store(":memory:") as store:
        r, clock = runs(store, settled("a"))
        r.ensure("roguelike", "chatpc")
        for _ in range(3):
            r.spend_life("rewind")
        die(r, clock)
        assert r.death is not None and r.death.vote_ends is None
        assert r.ballot("a", True, clock.now).ok is False
        assert r.verdict(clock.now) == "rip"


def test_the_death_card_the_final_blow_and_the_next_run() -> None:
    with Store(":memory:") as store:
        r, clock = runs(store, settled("yt:a", "dc:b"))
        run = r.ensure("roguelike", "chatpc")
        sent = [
            (T + 100, "yt:a", "Ann", "!click F7", Click("F7", 650, 650)),
            (T + 200, "dc:b", "Bo", "!yes", Meta("yes")),  # a vote: not a command that ran
            (T + 250, "dc:b", "Bo", "!click A1", Click("A1", 50, 50)),
            (T + 400, "yt:a", "Ann", "!click B2", Click("B2", 150, 150)),  # after it was last seen well
        ]
        for ts, user, name, raw, command in sent:
            store.seen_user(user, name, ts, frozenset())
            store.record_command(ts, user, raw, command, "accepted", None)
        clock.now = T + 500
        card = r.card("it froze", last_well=T + 300)
        assert [c["text"] for c in card["last_commands"]] == ["!click B2", "!click A1", "!click F7"]
        assert card["final_blow"]["name"] == "Bo" and card["final_blow"]["text"] == "!click A1"
        assert (card["number"], card["lived"], card["lives_used"]) == (1, 500, 0)
        r.end(card, "run-1.png")
        assert r.current is None
        ended = store.run(run["id"])
        assert ended is not None and (ended["cause"], ended["final_frame"]) == ("it froze", "run-1.png")
        assert ended["final_blow"]["user"] == "dc:b" and len(ended["last_commands"]) == 3
        second = r.start("roguelike", "chatpc")
        assert second["number"] == 2 and r.lives_left() == 3 and r.state == "alive"
        assert [x["number"] for x in store.runs()] == [2, 1]


def test_uptime_text() -> None:
    assert [uptime_text(s) for s in (59, 180, 7500, 3 * 86400 + 4 * 3600)] == [
        "0 min", "3 min", "2 h 05 min", "3 d 4 h"
    ]  # fmt: skip


def test_a_deletion_request_takes_a_name_out_of_the_graveyard() -> None:
    with Store(":memory:") as store:
        r, clock = runs(store)
        r.ensure("roguelike", "chatpc")
        for ts, user, name in ((T + 10, "yt:a", "Ann"), (T + 20, "dc:b", "Bo")):
            store.seen_user(user, name, ts, frozenset())
            store.record_command(ts, user, "!enter", parse_enter(), "accepted", None)
        clock.now = T + 30
        r.end(r.card("it froze", last_well=T + 25))
        store.forget(T + 40, "dc:b")
        [ended] = store.runs()
        assert ended["final_blow"] == {"ts": T + 20, "user": "", "name": "someone", "text": "!enter"}
        assert [(c["user"], c["name"]) for c in ended["last_commands"]] == [("", "someone"), ("yt:a", "Ann")]


def parse_enter() -> object:
    from chatuses.grid import Grid
    from chatuses.parser import parse

    return parse("!enter", Grid())
