"""From chat message to accepted command (PLAN.md 5.7, command flow):

    moderation gate (banned?) -> parser -> rate limits (input only) ->
    text filter -> accepted, for the referee and the executor

Every message counts for presence (who is active, who may vote). Every
command, accepted or not, is recorded with its outcome; ordinary chat isn't
stored. The text filter is Phase 5's (PLAN.md 7.4): until then the hook
lets everything through.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from chatuses.commands import Command, InputCommand, Load, ParseError, Save, TypeText
from chatuses.grid import Grid
from chatuses.identity import Presence
from chatuses.parser import parse
from chatuses.ratelimit import RateLimiter
from chatuses.store import Store

OutcomeKind = Literal["chat", "accepted", "invalid", "rate_limited", "filtered", "banned"]

# Returns a reason to refuse the text, or None to let it through.
TextFilter = Callable[[str, str], str | None]  # (user id, text)


def allow_all(_user: str, _text: str) -> str | None:
    return None


@dataclass(frozen=True)
class ChatMessage:
    user_id: str  # yt:<channel id>, dc:<user id>
    display_name: str
    text: str
    ts: float  # seconds


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    command: Command | None = None
    reason: str | None = None  # for chat: never contains the user's own text


class Pipeline:
    def __init__(
        self,
        store: Store,
        grid: Grid | None = None,
        limiter: RateLimiter | None = None,
        presence: Presence | None = None,
        text_filter: TextFilter = allow_all,
        is_banned: Callable[[str], bool] = lambda _user: False,
    ) -> None:
        self.store = store
        self.grid = grid or Grid()
        self.limiter = limiter or RateLimiter()
        self.presence = presence or Presence(is_banned=is_banned)
        self.text_filter = text_filter
        self.is_banned = is_banned

    def handle(self, msg: ChatMessage) -> Outcome:
        self.presence.saw(msg.user_id, msg.ts)
        parsed = parse(msg.text, self.grid)
        if parsed is None:
            return Outcome("chat")
        self.store.seen_user(msg.user_id, msg.display_name, msg.ts)
        outcome = self._judge(msg, parsed)
        self.store.record_command(
            msg.ts, msg.user_id, msg.text, outcome.command, outcome.kind, outcome.reason
        )
        return outcome

    def _judge(self, msg: ChatMessage, parsed: Command | ParseError) -> Outcome:
        if self.is_banned(msg.user_id):
            return Outcome("banned", reason="you can't send commands right now")
        if isinstance(parsed, ParseError):
            return Outcome("invalid", reason=parsed.reason)
        if isinstance(parsed, InputCommand):
            verdict = self.limiter.allow(msg.user_id, msg.ts)
            if verdict == "user":
                return Outcome("rate_limited", parsed, "slow down: one command every 2 seconds")
            if verdict == "global":
                return Outcome("rate_limited", parsed, "chat is busy: try again in a moment")
        text = _filterable_text(parsed)
        if text is not None and (reason := self.text_filter(msg.user_id, text)):
            return Outcome("filtered", parsed, reason)
        return Outcome("accepted", parsed)


def _filterable_text(command: Command) -> str | None:
    """Text from chat that ends up on screen: typed text and save names."""
    if isinstance(command, TypeText):
        return command.text
    if isinstance(command, (Load, Save)):
        return command.name
    return None
