from chatuses.commands import Click, TypeText
from chatuses.identity import Presence
from chatuses.referee.laws import Laws
from chatuses.referee.modes import Modes, Winner

T = 100_000.0


def room(n: int) -> Presence:
    """n people settled in (here 10 minutes, a message 10 s ago): u0, u1..."""
    presence = Presence()
    for i in range(n):
        presence.saw(f"u{i}", T - 600)
        presence.saw(f"u{i}", T - 10)
    return presence


def modes(presence: Presence, **laws: float) -> tuple[Modes, list[Winner]]:
    won: list[Winner] = []
    return Modes(presence, lambda: Laws(**laws), on_winner=won.append, now=T), won  # type: ignore[arg-type]


def test_pushes_move_the_meter_once_per_person_per_gap() -> None:
    m, _ = modes(room(3), meter_drift_s=0)
    assert m.push("u0", "democracy", T).ok and m.meter == 52
    assert m.push("u0", "democracy", T + 5).text == "one push every 10 s"
    assert m.push("u1", "anarchy", T + 5).ok and m.meter == 50
    assert m.push("u0", "democracy", T + 10).ok and m.meter == 52


def test_only_settled_people_push_the_meter() -> None:
    presence = room(1)
    presence.saw("raider", T - 30)
    m, _ = modes(presence)
    assert m.push("raider", "democracy", T).text == "you can push the meter once you've been here 2 minutes"
    assert m.meter == 50


def test_hysteresis_and_the_lock_after_a_switch() -> None:
    m, _ = modes(room(20), meter_drift_s=0)
    for i in range(15):  # 50 + 15 x 2 = 80: democracy
        m.push(f"u{i}", "democracy", T)
    assert (m.mode, m.meter) == ("democracy", 80)
    for i in range(15, 20):  # down to 70: still democracy (anarchy is at 20)
        m.push(f"u{i}", "anarchy", T + 1)
    assert m.mode == "democracy" and m.meter == 70
    for i in range(15):  # down to 40, then 20 within the lock: no switch yet
        m.push(f"u{i}", "anarchy", T + 20)
    for i in range(15, 20):
        m.push(f"u{i}", "anarchy", T + 30)
    assert m.meter == 30 and m.mode == "democracy"
    for i in range(5):
        m.push(f"u{i}", "anarchy", T + 40)  # 20
    assert m.meter == 20 and m.mode == "democracy"  # locked for 120 s after the switch at T
    m.tick(T + 119)
    assert m.mode == "democracy"
    m.tick(T + 120)
    assert m.mode == "anarchy" and m.locked_until == T + 240


def test_the_meter_drifts_back_to_the_centre() -> None:
    m, _ = modes(room(10), meter_step=10)
    for i in range(2):
        m.push(f"u{i}", "democracy", T)  # 70
    m.tick(T + 35)  # 3 points in 35 s
    assert m.meter == 67
    m.tick(T + 10_000)  # never past the centre
    assert m.meter == 50
    m.presence.saw("u2", T + 10_000)  # active again
    assert m.push("u2", "anarchy", T + 10_001).ok and m.meter == 40
    m.tick(T + 10_011)
    assert m.meter == 41  # from below, too


def test_democracy_runs_the_most_voted_command_ties_to_the_earliest() -> None:
    m, won = modes(room(5))
    m.set_mode("democracy", T)
    click, other, typed = Click("F7", 650, 650), Click("A1", 50, 50), TypeText("hi")
    m.ballot("u0", "Ann", other, T)
    m.ballot("u1", "Bob", click, T + 1)
    m.ballot("u2", "Cy", click, T + 2)
    m.ballot("u3", "Di", other, T + 3)
    m.ballot("u4", "Ed", typed, T + 4)
    m.ballot("u4", "Ed", click, T + 5)  # changed their mind: their latest counts
    assert [(t, n) for t, n, _ in m.tally()] == [("!click F7", 3), ("!click A1", 2)]
    assert m.window_ends == T + 12  # from the first ballot
    m.tick(T + 11)
    assert won == []
    m.tick(T + 12)
    assert [(w.text, w.votes, w.total, w.ballot.name) for w in won] == [("!click F7", 3, 5, "Bob")]
    assert m.ballots == {} and m.window_ends is None
    m.ballot("u0", "Ann", other, T + 20)
    m.ballot("u1", "Bob", click, T + 21)  # a tie: the earliest wins
    m.tick(T + 32)
    assert won[-1].text == "!click A1"


def test_a_moderator_sets_the_mode_and_a_panic_clears_the_ballots() -> None:
    m, won = modes(room(2))
    assert m.set_mode("democracy", T).text == "Mode: democracy (locked for 120 s)."
    assert m.meter == 80 and m.set_mode("democracy", T).text == "It's already democracy."
    m.ballot("u0", "Ann", TypeText("x"), T + 1)
    m.clear()
    m.tick(T + 20)
    assert won == []
    assert m.set_mode("anarchy", T + 30).ok and m.meter == 20 and m.mode == "anarchy"


def test_the_overlay_view() -> None:
    m, _ = modes(room(2))
    assert m.view(T) == {
        "mode": "anarchy", "meter": 50, "democracy_at": 80, "anarchy_at": 20, "locked_for": 0, "tally": None
    }  # fmt: skip
    m.set_mode("democracy", T)
    m.ballot("u0", "Ann", TypeText("x"), T + 2)
    view = m.view(T + 5)
    assert view["mode"] == "democracy" and view["locked_for"] == 115
    assert view["tally"] == {"ends_in": 9.0, "options": [{"text": "!type x", "votes": 1}], "total": 1}
