"""From chat message to accepted command (PLAN.md 5.7, command flow):

    moderation gate (banned or timed out? kept out by lockdown?) -> parser
    -> paused (input during a panic) -> rate limits (input only) -> text
    filter -> accepted, for the referee and the executor

Every message counts for presence (who is active, who may vote). Every
command, accepted or not, is recorded with its outcome; ordinary chat isn't
stored. The text filter (PLAN.md 7.4, safety/textfilter.py) sees typed text
together with what was typed just before, save names, and a macro's name and
text when it's proposed and its text again when it runs; a refused slur is
a strike, and 3 strikes in 10 minutes a 10-minute timeout.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from chatuses.commands import (
    ClickText,
    Command,
    InputCommand,
    Load,
    MacroDefine,
    MacroRun,
    ParseError,
    PressKeys,
    Save,
    StepCommand,
    TypeText,
)
from chatuses.grid import Grid
from chatuses.identity import Presence
from chatuses.parser import Macros, parse
from chatuses.ratelimit import RateLimiter
from chatuses.safety.strikes import Strikes
from chatuses.safety.textfilter import HATE, PERSONAL, Kind, TextFilter
from chatuses.store import Store

OutcomeKind = Literal["chat", "accepted", "invalid", "rate_limited", "filtered", "banned", "locked", "paused"]
LOCKED = "lockdown: only regulars can send commands right now"
PAUSED = "the machine is paused"
REASONS: dict[Kind, str] = {"hate": HATE, "personal": PERSONAL}


class Censor(Protocol):
    """What the pipeline needs from the text filter (TextFilter; tests use stand-ins)."""

    def kind(self, text: str) -> Kind | None: ...
    def typed_kind(self, text: str) -> Kind | None: ...
    def typed(self, text: str) -> None: ...
    def key(self, name: str, times: int = 1) -> None: ...


@dataclass(frozen=True)
class ChatMessage:
    user_id: str  # yt:<channel id>, dc:<user id>
    display_name: str
    text: str
    ts: float  # seconds
    # What the platform says about the author: owner, moderator, member
    # (YouTube), regular (Discord), verified. Used by later phases' moderator
    # commands; recorded with the user.
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    command: Command | None = None
    reason: str | None = None  # for chat: never contains the user's own text
    timed_out_until: float | None = None  # this refusal earned a timeout, until then


class Pipeline:
    def __init__(
        self,
        store: Store,
        grid: Grid | None = None,
        limiter: RateLimiter | None = None,
        presence: Presence | None = None,
        censor: Censor | None = None,
        strikes: Strikes | None = None,
        is_banned: Callable[[str], bool] = lambda _user: False,
        locked_out: Callable[[ChatMessage], bool] = lambda _msg: False,
        paused: Callable[[], bool] = lambda: False,
        macros: Callable[[], Macros] = dict,
    ) -> None:
        self.store = store
        self.grid = grid or Grid()
        self.limiter = limiter or RateLimiter()
        self.presence = presence or Presence(is_banned=is_banned)
        self.censor: Censor = censor or TextFilter()
        self.strikes = strikes or Strikes()
        self.is_banned = is_banned
        self.locked_out = locked_out  # lockdown (safety/moderation.py)
        self.paused = paused  # panic: input is frozen
        self.macros = macros  # the macros chat voted in (brain's)

    def handle(self, msg: ChatMessage) -> Outcome:
        self.presence.saw(msg.user_id, msg.ts)
        parsed = parse(msg.text, self.grid, self.macros())
        if parsed is None:
            return Outcome("chat")
        self.store.seen_user(msg.user_id, msg.display_name, msg.ts, msg.roles)
        outcome = self._judge(msg, parsed)
        self.store.record_command(
            msg.ts, msg.user_id, msg.text, outcome.command, outcome.kind, outcome.reason
        )
        if outcome.kind == "accepted":
            self._remember(outcome.command)
        return outcome

    def _remember(self, command: Command | None) -> None:
        """Tell the filter what's being typed on screen."""
        if isinstance(command, MacroRun):
            for step in command.steps:
                self._remember(step)
        elif isinstance(command, TypeText):
            self.censor.typed(command.text)
        elif isinstance(command, PressKeys):
            for _ in range(command.repeat):
                for combo in command.combos:
                    if not combo.modifiers:
                        self.censor.key(combo.text)

    def _judge(self, msg: ChatMessage, parsed: Command | ParseError) -> Outcome:
        if self.is_banned(msg.user_id) or self.store.banned_until(msg.user_id) > msg.ts:
            return Outcome("banned", reason="you can't send commands right now")
        if self.locked_out(msg):
            return Outcome("locked", reason=LOCKED)
        if isinstance(parsed, ParseError):
            return Outcome("invalid", reason=parsed.reason)
        if isinstance(parsed, InputCommand) and self.paused():
            return Outcome("paused", parsed, PAUSED)
        if isinstance(parsed, InputCommand):
            verdict = self.limiter.allow(msg.user_id, msg.ts)
            if verdict == "user":
                return Outcome("rate_limited", parsed, "slow down: one command every 2 seconds")
            if verdict == "global":
                return Outcome("rate_limited", parsed, "chat is busy: try again in a moment")
        kind = self._filter(parsed)
        if kind is None:
            return Outcome("accepted", parsed)
        until = self.strikes.add(msg.user_id, msg.ts) if kind == "hate" else None
        if until is not None:
            self.store.time_out(msg.ts, msg.user_id, until, "3 refused slurs in 10 minutes")
        return Outcome("filtered", parsed, REASONS[kind], until)

    def _filter(self, command: Command) -> Kind | None:
        """Text from chat that ends up on screen: typed text (with what came just before), save names."""
        if isinstance(command, TypeText):
            return self.censor.typed_kind(command.text)
        if isinstance(command, (Load, Save)):
            return self.censor.kind(command.name)
        if isinstance(command, ClickText):  # the log shows it
            return self.censor.kind(command.text)
        if isinstance(command, MacroDefine):  # shown on the overlay as it's voted on
            return self.censor.kind(command.name) or next(
                (kind for text in _typed(command.steps) if (kind := self.censor.kind(text))), None
            )
        if isinstance(command, MacroRun) and (typed := "".join(_typed(command.steps))):
            return self.censor.typed_kind(typed)  # with what was typed just before
        return None


def _typed(steps: tuple[StepCommand, ...]) -> list[str]:
    return [step.text for step in steps if isinstance(step, TypeText)]
