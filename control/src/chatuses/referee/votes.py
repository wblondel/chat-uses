"""Power votes (PLAN.md 6.4, 6.5): reboot, reset, save, rewind, load, and
later newrun and impeach.

- One vote at a time; the proposer's yes counts.
- Who may vote is fixed when it opens, at T: users active at T (a message in
  the 5 minutes before) and present in this session since T - 2 min. A raid
  of new accounts can't swing a vote, nor can people who weren't around.
  The proposer must be one of them too.
- Quorum = max(floor, ceil(fraction x users active at T)) yes votes; it
  passes with at least that many yes and yes / (yes + no) >= threshold.
- Some tiers then give chat a veto countdown: the action is cancelled if
  vetoes reach max(veto_min, veto_fraction x yes).
- Once decided, the action can't be proposed again for its cooldown.

Everything here is driven by `now` (seconds): brain calls tick() a few times
a second; tests pass a fake clock. What the action does is brain's (it is
told by on_pass and reports back with finished()).
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from chatuses.commands import StepCommand
from chatuses.identity import Presence
from chatuses.referee.laws import Laws, Tier

Phase = Literal["voting", "veto", "running", "failed", "vetoed", "cancelled", "done", "error"]
OPEN: tuple[Phase, ...] = ("voting", "veto")
SHOW_RESULT = 8.0  # seconds the overlay keeps showing how a vote ended

NOT_ELIGIBLE = "votes count from people who were here 2 minutes before it opened"
NO_VOTE = "no vote running"


class VoteLog(Protocol):
    """Where votes are kept (store.Store)."""

    def open_vote(
        self, kind: str, proposer: str, opened: float, params: dict[str, Any], eligible: int
    ) -> int: ...
    def close_vote(self, vote_id: int, closed: float, tally: dict[str, Any], result: str) -> None: ...


@dataclass(frozen=True)
class Proposal:
    action: str  # reboot, save, reset, rewind, load, newrun, impeach, macro, unmacro
    text: str  # as chat writes it (commands.describe): "!rewind 2"
    arg: int | str | None = None  # rewind: how many back; save, load, macro, unmacro: the name
    steps: tuple[StepCommand, ...] = ()  # macro: what it will run


@dataclass(frozen=True)
class Reply:
    ok: bool
    text: str  # for chat: fixed wording, never what chat wrote


@dataclass
class Vote:
    id: int
    proposal: Proposal
    proposer: str
    proposer_name: str
    opened: float
    tier: Tier
    active: int  # users active when it opened
    quorum: int
    eligible: frozenset[str]
    ballots: dict[str, bool] = field(default_factory=dict[str, bool])
    vetoes: set[str] = field(default_factory=set[str])
    phase: Phase = "voting"
    ends: float = 0.0  # end of the voting window, or of the veto countdown
    closed: float | None = None
    result: str = ""  # how it ended, in a few words

    @property
    def yes(self) -> int:
        return sum(self.ballots.values())

    @property
    def no(self) -> int:
        return len(self.ballots) - self.yes


class VoteEngine:
    def __init__(
        self,
        presence: Presence,
        laws: Callable[[], Laws],
        log: VoteLog,
        on_change: Callable[["Vote | None"], None] = lambda _vote: None,
        on_pass: Callable[[Vote], None] = lambda _vote: None,
    ) -> None:
        self.presence = presence
        self.laws = laws
        self.log = log
        self.on_change = on_change  # the overlay shows it
        self.on_pass = on_pass  # brain carries it out, then calls finished()
        self.current: Vote | None = None
        self._cooldown: dict[str, float] = {}  # action -> when it may be proposed again

    # --- chat ------------------------------------------------------------------------

    def propose(self, user: str, name: str, proposal: Proposal, now: float) -> Reply:
        vote = self.current
        if vote is not None and vote.phase in OPEN:
            return Reply(False, "a vote is already running: !yes or !no")
        if vote is not None and vote.phase == "running":
            return Reply(False, f"wait: {vote.proposal.text} is happening")
        if (until := self._cooldown.get(proposal.action, 0.0)) > now:
            return Reply(False, f"!{proposal.action} was just voted on: again in {_wait(until - now)}")
        if not self.presence.is_eligible(user, now):
            return Reply(False, "you can propose votes once you've been here 2 minutes")
        active = self.presence.active(now)
        eligible = frozenset(u for u in active if self.presence.is_eligible(u, now))
        tier = self.laws().tier(proposal.action)
        params: dict[str, Any] = {"text": proposal.text, "arg": proposal.arg}
        vote_id = self.log.open_vote(proposal.action, user, now, params, len(eligible))
        self.current = Vote(
            vote_id,
            proposal,
            user,
            name,
            now,
            tier,
            len(active),
            tier.quorum(len(active)),
            eligible,
            {user: True},
            ends=now + tier.window_s,
        )
        self.on_change(self.current)
        return Reply(True, f"vote open: {proposal.text}")

    def ballot(self, user: str, yes: bool, now: float) -> Reply:
        vote = self.current
        if vote is None or vote.phase not in OPEN:
            return Reply(False, NO_VOTE)
        if vote.phase == "veto":
            return Reply(False, "voting is over: !veto to stop it")
        if user not in vote.eligible:
            return Reply(False, NOT_ELIGIBLE)
        vote.ballots[user] = yes
        self.on_change(vote)
        return Reply(True, "yes" if yes else "no")

    def veto(self, user: str, now: float) -> Reply:
        vote = self.current
        if vote is None or vote.phase != "veto":
            return Reply(False, "nothing to veto right now")
        if user not in vote.eligible:
            return Reply(False, NOT_ELIGIBLE)
        vote.vetoes.add(user)
        if len(vote.vetoes) >= self.veto_needed(vote):
            self._close(vote, "vetoed", "vetoed by chat", now)
        else:
            self.on_change(vote)
        return Reply(True, "veto")

    # --- moderators and brain --------------------------------------------------------

    def cancel(self, why: str, now: float) -> Reply:
        """End the vote now (a moderator's /endvote, a panic). Not an action already running."""
        vote = self.current
        if vote is None or vote.phase not in OPEN:
            return Reply(False, NO_VOTE)
        self._close(vote, "cancelled", why, now)
        return Reply(True, f"Vote ended: {vote.proposal.text} ({why}).")

    def finished(self, vote_id: int, ok: bool, text: str, now: float) -> None:
        """brain carried out (or couldn't) what the vote passed."""
        vote = self.current
        if vote is None or vote.id != vote_id or vote.phase != "running":
            return
        self._close(vote, "done" if ok else "error", text, now)

    def tick(self, now: float) -> None:
        vote = self.current
        if vote is None:
            return
        if vote.phase == "voting" and now >= vote.ends:
            self._count(vote, now)
        elif vote.phase == "veto" and now >= vote.ends:
            self._run(vote, now)
        elif vote.closed is not None and now - vote.closed >= SHOW_RESULT:
            self.current = None
            self.on_change(None)

    def veto_needed(self, vote: Vote) -> int:
        laws = self.laws()
        return max(laws.veto_min, math.ceil(laws.veto_fraction * vote.yes))

    def view(self, now: float) -> dict[str, Any] | None:
        """For the overlay: what's being voted, the count, the time left."""
        vote = self.current
        if vote is None:
            return None
        return {
            "id": vote.id,
            "text": vote.proposal.text,
            "by": vote.proposer_name,
            "phase": vote.phase,
            "ends_in": round(max(0.0, vote.ends - now), 1) if vote.phase in OPEN else 0,
            "yes": vote.yes,
            "no": vote.no,
            "quorum": vote.quorum,
            "threshold": vote.tier.threshold,
            "vetoes": len(vote.vetoes),
            "veto_needed": self.veto_needed(vote),
            "result": vote.result,
        }

    # --- inside ----------------------------------------------------------------------

    def _count(self, vote: Vote, now: float) -> None:
        yes, no = vote.yes, vote.no
        if yes < vote.quorum:
            self._close(vote, "failed", f"not enough votes ({yes} yes of {vote.quorum} needed)", now)
        elif yes / (yes + no) < vote.tier.threshold:
            share = round(100 * yes / (yes + no))
            self._close(vote, "failed", f"{share}% yes, {round(100 * vote.tier.threshold)}% needed", now)
        elif vote.tier.veto_s > 0:
            vote.phase, vote.ends = "veto", now + vote.tier.veto_s
            self._cooldown[vote.proposal.action] = now + vote.tier.cooldown_min * 60
            self.on_change(vote)
        else:
            self._run(vote, now)

    def _run(self, vote: Vote, now: float) -> None:
        vote.phase, vote.result = "running", "passed"
        self._cooldown[vote.proposal.action] = now + vote.tier.cooldown_min * 60
        self.on_change(vote)
        self.on_pass(vote)

    def _close(self, vote: Vote, phase: Phase, result: str, now: float) -> None:
        vote.phase, vote.result, vote.closed = phase, result, now
        action = vote.proposal.action
        self._cooldown[action] = max(self._cooldown.get(action, 0.0), now + vote.tier.cooldown_min * 60)
        tally = {
            "yes": vote.yes,
            "no": vote.no,
            "vetoes": len(vote.vetoes),
            "quorum": vote.quorum,
            "active": vote.active,
            "result": result,
        }
        self.log.close_vote(vote.id, now, tally, phase)
        self.on_change(vote)


def _wait(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    return f"{round(seconds / 60)} min"
