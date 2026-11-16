"""The referee (PLAN.md 5.7): chat's meta commands, under the ruleset's laws.

Takes the meta commands the pipeline accepted (commands.MetaCommand):
proposals for power votes, ballots and vetoes go to the vote engine
(votes.py); what a passed vote does is brain's (`Actions`). Democracy mode
(modes.py) comes with Phase 6 step 3.

The ruleset comes from rulesets/NAME.yaml (laws.py); the laws are its laws
with the store's `laws` table on top. Switching rulesets or changing a law
applies to votes opened from then on.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from chatuses.commands import Load, Meta, MetaCommand, Rewind, Save, describe
from chatuses.identity import Presence
from chatuses.referee import laws as laws_module
from chatuses.referee.laws import DEFAULT, Laws, Ruleset, RulesetError
from chatuses.referee.votes import Proposal, Reply, Vote, VoteEngine
from chatuses.store import Store

log = logging.getLogger(__name__)

# Built in brain so far (the rest of a ruleset's features come later).
BUILT = frozenset({"anarchy", "power_votes", "snapshots"})
NEEDS_FEATURE = {"save": "snapshots", "rewind": "snapshots", "load": "snapshots"}


class Actions(Protocol):
    """What brain does for a passed vote."""

    def unavailable(self, proposal: Proposal) -> str | None: ...  # why it can't happen now (None: it can)
    async def run(self, proposal: Proposal) -> tuple[bool, str]: ...  # done?, a few words for chat


class Referee:
    def __init__(
        self,
        store: Store,
        presence: Presence,
        actions: Actions,
        show: Callable[[dict[str, Any] | None], None] = lambda _view: None,
        rulesets: Path | None = None,
        ruleset: Ruleset = DEFAULT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.actions = actions
        self.show = show  # the overlay's vote panel
        self.rulesets = rulesets
        self.clock = clock
        self.ruleset = ruleset
        self.laws = self._laws(ruleset)
        self.votes = VoteEngine(presence, lambda: self.laws, store, self._changed, self._passed)
        self._running: set[asyncio.Task[None]] = set()

    def _laws(self, ruleset: Ruleset) -> Laws:
        try:
            return laws_module.apply(ruleset.laws, dict(self.store.laws()))
        except RulesetError as err:  # a law the ruleset now bounds differently: the ruleset's wins
            log.error("laws table: %s (using the ruleset's laws)", err)
            return ruleset.laws

    # --- chat ------------------------------------------------------------------------

    def handle(self, user: str, name: str, command: MetaCommand, now: float) -> Reply:
        """A meta command from chat (not !grid, !help, !rules: those are the overlay's)."""
        word = _word(command)
        if not self.ruleset.allows(word):
            return Reply(False, f"!{word} is off in this ruleset")
        match command:
            case Meta(name="yes" | "no"):
                return self.votes.ballot(user, command.name == "yes", now)
            case Meta(name="veto"):
                return self.votes.veto(user, now)
            case Meta(name="anarchy" | "democracy"):
                return Reply(False, "democracy mode is coming soon")
            case Meta(name=("reboot" | "reset" | "newrun" | "impeach") as action):
                return self._propose(user, name, Proposal(action, describe(command)), now)
            case Rewind(steps=steps):
                return self._propose(user, name, Proposal("rewind", describe(command), steps), now)
            case Load(name=save):
                return self._propose(user, name, Proposal("load", describe(command), save), now)
            case Save(name=save):
                return self._propose(user, name, Proposal("save", describe(command), save), now)
            case Meta():
                return Reply(False, "not a vote")

    def _propose(self, user: str, name: str, proposal: Proposal, now: float) -> Reply:
        if "power_votes" not in self.ruleset.features:
            return Reply(False, "power votes are off in this ruleset")
        feature = NEEDS_FEATURE.get(proposal.action)
        if feature and feature not in self.ruleset.features:
            return Reply(False, f"{feature} are off in this ruleset")
        current = self.votes.current
        busy = current is not None and current.phase in ("voting", "veto", "running")
        if not busy and (why := self.actions.unavailable(proposal)):  # a vote running says so first
            return Reply(False, why)
        return self.votes.propose(user, name, proposal, now)

    def tick(self) -> None:
        self.votes.tick(self.clock())

    # --- moderators and operators ----------------------------------------------------

    def end_vote(self, why: str) -> Reply:
        return self.votes.cancel(why, self.clock())

    def set_ruleset(self, name: str, by: str) -> str:
        """Switch to rulesets/NAME.yaml; raises RulesetError (the old one stays)."""
        if self.rulesets is None:
            raise RulesetError("no rulesets directory")
        ruleset = laws_module.load(self.rulesets, name)
        self.ruleset, self.laws = ruleset, self._laws(ruleset)
        self.store.set_setting("ruleset", name)
        self.store.event(self.clock(), "ruleset", {"name": name, "by": by})
        return f"Ruleset: {name}"

    def set_law(self, key: str, value: str, by: str) -> str:
        """A law over the ruleset's (`default` puts the ruleset's back); raises RulesetError."""
        if value == "default":
            if key not in self.store.laws():
                raise RulesetError(f"{key} is already the ruleset's")
            self.store.set_law(self.clock(), key, None, by)
            self.laws = self._laws(self.ruleset)
            return f"{key}: back to the ruleset's"
        try:
            number = float(value)
        except ValueError as err:
            raise RulesetError(f"{value!r} isn't a number") from err
        laws = laws_module.apply(self.laws, {key: int(number) if number.is_integer() else number})
        self.store.set_law(self.clock(), key, number, by)
        self.laws = laws
        return f"{key} = {value} (until the ruleset's comes back with `law {key} default`)"

    def status(self) -> dict[str, Any]:
        unbuilt = sorted(self.ruleset.features - BUILT)
        return {
            "ruleset": self.ruleset.name,
            "features": sorted(self.ruleset.features & BUILT),
            "not_built_yet": unbuilt,
            "disabled": sorted(self.ruleset.disabled),
            "laws_changed": self.store.laws(),
            "vote": self.votes.view(self.clock()),
        }

    def status_line(self) -> str:
        vote = self.votes.view(self.clock())
        if vote is None:
            what = "no vote"
        elif vote["phase"] in ("voting", "veto"):
            count = f"{vote['yes']} yes, {vote['no']} no, {vote['ends_in']:.0f} s left"
            what = f"{vote['text']} ({vote['phase']}, {count})"
        else:
            what = f"{vote['text']}: {vote['phase']}, {vote['result']}"
        changed = len(self.store.laws())
        return f"Ruleset: {self.ruleset.name}{f' ({changed} laws changed)' if changed else ''}; vote: {what}"

    # --- the vote engine's callbacks ------------------------------------------------

    def _changed(self, _vote: Vote | None) -> None:
        self.show(self.votes.view(self.clock()))

    def _passed(self, vote: Vote) -> None:
        task = asyncio.get_running_loop().create_task(self._carry_out(vote))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _carry_out(self, vote: Vote) -> None:
        try:
            ok, text = await self.actions.run(vote.proposal)
        except Exception as err:  # a failed action must still close the vote
            log.exception("vote %d: %s failed", vote.id, vote.proposal.text)
            ok, text = False, f"failed ({type(err).__name__})"
        self.votes.finished(vote.id, ok, text, self.clock())


def _word(command: MetaCommand) -> str:
    match command:
        case Meta(name=name):
            return name
        case Rewind():
            return "rewind"
        case Load():
            return "load"
        case Save():
            return "save"
