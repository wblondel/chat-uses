from typing import Any

from chatuses.identity import Presence
from chatuses.referee.laws import Laws
from chatuses.referee.votes import SHOW_RESULT, Proposal, Vote, VoteEngine

T = 100_000.0  # when votes open in these tests


class Log:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str, int]] = []
        self.closed: list[tuple[int, str, dict[str, Any]]] = []

    def open_vote(
        self, kind: str, proposer: str, opened: float, params: dict[str, Any], eligible: int
    ) -> int:
        self.opened.append((kind, proposer, eligible))
        return len(self.opened)

    def close_vote(self, vote_id: int, closed: float, tally: dict[str, Any], result: str) -> None:
        self.closed.append((vote_id, result, tally))


def room(n: int, banned: frozenset[str] = frozenset()) -> Presence:
    """n people here for 10 minutes, each with a message 10 s before T: u0, u1..."""
    presence = Presence(is_banned=lambda user: user in banned)
    for i in range(n):
        presence.saw(f"u{i}", T - 600)
        presence.saw(f"u{i}", T - 10)
    return presence


def engine(presence: Presence, laws: Laws | None = None) -> tuple[VoteEngine, Log, list[Vote]]:
    log, passed = Log(), list[Vote]()
    return VoteEngine(presence, lambda: laws or Laws(), log, on_pass=passed.append), log, passed


REBOOT = Proposal("reboot", "!reboot")  # 50 %, quorum 15 % (at least 2), 30 s, no veto, 2 min cooldown
RESET = Proposal("reset", "!reset")  # 60 %, quorum 20 % (at least 3)
REWIND = Proposal("rewind", "!rewind", 1)  # 66 %, quorum 25 % (at least 3), 30 s veto countdown


def test_a_vote_passes_and_is_carried_out() -> None:
    votes, log, passed = engine(room(4))
    assert votes.propose("u0", "Alice", REBOOT, T).ok
    assert log.opened == [("reboot", "u0", 4)]
    assert votes.ballot("u1", True, T + 5).ok
    votes.tick(T + 29)
    assert passed == []  # the window is 30 s
    votes.tick(T + 30)
    assert [v.proposal for v in passed] == [REBOOT] and votes.current and votes.current.phase == "running"
    votes.finished(passed[0].id, True, "rebooted", T + 35)
    assert votes.current.phase == "done" and log.closed[0][1] == "done"
    assert log.closed[0][2] | {"result": None} == {
        "yes": 2, "no": 0, "vetoes": 0, "quorum": 2, "active": 4, "result": None
    }  # fmt: skip
    votes.tick(T + 35 + SHOW_RESULT)
    assert votes.current is None  # the overlay stops showing it


def test_the_proposer_counts_as_yes_but_alone_misses_the_quorum() -> None:
    votes, _, passed = engine(room(4))
    votes.propose("u0", "Alice", REBOOT, T)
    votes.tick(T + 30)
    assert passed == [] and votes.current and votes.current.phase == "failed"
    assert votes.current.result == "not enough votes (1 yes of 2 needed)"


def test_quorum_grows_with_the_crowd_and_threshold_applies_to_yes_and_no() -> None:
    votes, _, _ = engine(room(20))  # reset: max(3, 20 % of 20) = 4
    votes.propose("u0", "Alice", RESET, T)
    assert votes.current and votes.current.quorum == 4
    for i in (1, 2, 3):
        votes.ballot(f"u{i}", True, T + 1)
    for i in (4, 5, 6):
        votes.ballot(f"u{i}", False, T + 1)
    votes.tick(T + 30)  # 4 yes, 3 no: 57 % < 60 %
    assert votes.current.phase == "failed" and votes.current.result == "57% yes, 60% needed"


def test_a_tie_passes_a_fifty_percent_vote() -> None:
    votes, _, passed = engine(room(6))
    votes.propose("u0", "Alice", REBOOT, T)
    votes.ballot("u1", True, T + 1)
    votes.ballot("u2", False, T + 1)
    votes.ballot("u3", False, T + 1)
    votes.tick(T + 30)
    assert len(passed) == 1


def test_who_may_vote_is_fixed_when_it_opens() -> None:
    presence = room(4)
    presence.saw("raider", T - 30)  # arrived 30 s before: not settled
    presence.saw("lurker", T - 3600)  # here for an hour, quiet for 6 minutes: not active
    presence.saw("lurker", T - 360)
    votes, _, _ = engine(presence)
    votes.propose("u0", "Alice", REBOOT, T)
    for user in ("raider", "lurker"):
        presence.saw(user, T + 5)  # speaking up now doesn't help
        assert (
            votes.ballot(user, True, T + 5).text
            == "votes count from people who were here 2 minutes before it opened"
        )
    presence.saw("u3", T + 5)  # an eligible voter's own message (it updates presence) doesn't lose the vote
    assert votes.ballot("u3", True, T + 5).ok
    assert votes.current and votes.current.yes == 2


def test_a_newcomer_cant_propose_and_banned_users_dont_count() -> None:
    presence = room(4, banned=frozenset({"u3"}))
    presence.saw("raider", T - 30)
    votes, _, _ = engine(presence)
    assert (
        votes.propose("raider", "Raider", REBOOT, T).text
        == "you can propose votes once you've been here 2 minutes"
    )
    assert votes.propose("u0", "Alice", REBOOT, T).ok
    # Active (6.5): a message in the last 5 minutes, not banned. The raider is
    # active (it counts toward the quorum) but too new to vote; u3 is banned.
    assert votes.current and votes.current.active == 4
    assert votes.current.eligible == frozenset({"u0", "u1", "u2"})
    assert not votes.ballot("u3", True, T + 1).ok


def test_the_last_ballot_counts() -> None:
    votes, _, _ = engine(room(4))
    votes.propose("u0", "Alice", REBOOT, T)
    votes.ballot("u1", True, T + 1)
    votes.ballot("u1", False, T + 2)
    assert votes.current and (votes.current.yes, votes.current.no) == (1, 1)


def test_one_vote_at_a_time() -> None:
    votes, _, passed = engine(room(4))
    votes.propose("u0", "Alice", REBOOT, T)
    assert votes.propose("u1", "Bob", RESET, T + 1).text == "a vote is already running: !yes or !no"
    votes.ballot("u1", True, T + 1)
    votes.tick(T + 30)
    assert votes.propose("u1", "Bob", RESET, T + 31).text == "wait: !reboot is happening"
    votes.finished(passed[0].id, True, "rebooted", T + 40)
    assert votes.propose("u1", "Bob", RESET, T + 41).ok  # another action: no cooldown


def test_cooldown_after_a_vote_whatever_its_result() -> None:
    votes, _, _ = engine(room(4))
    votes.propose("u0", "Alice", REBOOT, T)
    votes.tick(T + 30)  # failed
    assert votes.propose("u1", "Bob", REBOOT, T + 40).text == "!reboot was just voted on: again in 2 min"
    assert votes.propose("u1", "Bob", REBOOT, T + 30 + 60).text == "!reboot was just voted on: again in 60 s"
    assert votes.propose("u1", "Bob", REBOOT, T + 30 + 120).ok


def test_veto_countdown_cancels_a_passed_vote() -> None:
    votes, log, passed = engine(room(12))
    votes.propose("u0", "Alice", REWIND, T)
    for i in range(1, 7):
        votes.ballot(f"u{i}", True, T + 1)  # 7 yes
    votes.tick(T + 30)
    assert votes.current and votes.current.phase == "veto" and passed == []
    assert votes.veto_needed(votes.current) == 3  # max(3, 7/3)
    assert not votes.ballot("u8", False, T + 31).ok  # too late for no
    votes.veto("u8", T + 32)
    votes.veto("u9", T + 33)
    assert votes.current.phase == "veto"
    votes.veto("u10", T + 34)
    assert votes.current.phase == "vetoed" and votes.current.result == "vetoed by chat"
    assert log.closed[0][1] == "vetoed" and passed == []
    assert not votes.propose("u0", "Alice", REWIND, T + 40).ok  # its cooldown runs


def test_a_veto_countdown_without_enough_vetoes_carries_it_out() -> None:
    votes, _, passed = engine(room(12))
    votes.propose("u0", "Alice", REWIND, T)
    for i in range(1, 9):
        votes.ballot(f"u{i}", True, T + 1)  # 9 yes: 3 vetoes needed...
    votes.tick(T + 30)
    votes.veto("u9", T + 31)
    votes.veto("u10", T + 31)
    votes.tick(T + 59)
    assert passed == []
    votes.tick(T + 60)
    assert len(passed) == 1 and passed[0].phase == "running"


def test_many_yes_votes_need_more_vetoes() -> None:
    votes, _, _ = engine(room(40))
    votes.propose("u0", "Alice", REWIND, T)
    for i in range(1, 15):
        votes.ballot(f"u{i}", True, T + 1)  # 15 yes
    votes.tick(T + 30)
    assert votes.current and votes.veto_needed(votes.current) == 5


def test_a_moderator_ends_a_vote_but_not_one_being_carried_out() -> None:
    votes, log, passed = engine(room(4))
    assert not votes.cancel("by Mod", T).ok
    votes.propose("u0", "Alice", REBOOT, T)
    reply = votes.cancel("ended by Mod", T + 3)
    assert reply.ok and reply.text == "Vote ended: !reboot (ended by Mod)."
    assert votes.current and votes.current.phase == "cancelled" and log.closed[0][1] == "cancelled"
    votes.tick(T + 3 + SHOW_RESULT)
    votes.propose("u1", "Bob", RESET, T + 20)
    votes.ballot("u0", True, T + 21)
    votes.ballot("u2", True, T + 21)
    votes.tick(T + 50)
    assert len(passed) == 1 and not votes.cancel("ended by Mod", T + 51).ok


def test_the_overlay_view() -> None:
    votes, _, _ = engine(room(12))
    assert votes.view(T) is None
    votes.propose("u0", "Alice", REWIND, T)
    votes.ballot("u1", False, T + 2)
    assert votes.view(T + 10.04) == {
        "id": 1,
        "text": "!rewind",
        "by": "Alice",
        "phase": "voting",
        "ends_in": 20.0,
        "yes": 1,
        "no": 1,
        "quorum": 3,
        "threshold": 0.66,
        "vetoes": 0,
        "veto_needed": 3,
        "result": "",
    }
