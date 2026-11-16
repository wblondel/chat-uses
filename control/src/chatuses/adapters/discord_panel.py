"""The Discord control panel (PLAN.md D9, Phase 6): one message in #control,
with Yes, No, Veto, Anarchy and Democracy buttons, that brain keeps up to date.

- A press is the presser typing the button's command (`!yes`…) in #control:
  the same identity, roles, bans, lockdown, rate limits and voting rules.
  Only they see the answer, so presses don't fill the channel.
- One message, edited in place when what it shows changes, at most once
  every EVERY seconds (Discord limits edits). Countdowns are Discord
  timestamps, which tick in each viewer's app without edits. Buttons that
  can't do anything right now are greyed out.
- When chat has buried it (BURIED messages under it) it moves to the bottom:
  posted again, then the old one deleted, at most once every MOVE_GAP
  seconds, so it doesn't jump away from someone about to press.
- At start, panels an earlier run left (in the last HISTORY messages) are
  deleted; on a clean stop the panel says brain is offline, buttons greyed.

Every panel message has the same button IDs, and the bot listens for them
from the start (a persistent view), so a press on any of them works.
"""

import asyncio
import contextlib
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

log = logging.getLogger(__name__)

PREFIX = "chatuses:panel:"  # button IDs: chatuses:panel:yes…
EVERY = 2.0  # seconds between looks at what the panel shows
BURIED = 15  # messages under the panel before it moves to the bottom
MOVE_GAP = 60.0  # seconds between moves, at least
HISTORY = 100  # messages searched for old panels at start
CLOSE_TIMEOUT = 3.0  # seconds to say "offline" when brain stops

TITLE = "🕹️ Control panel"
FOOTER = (
    "The buttons do what typing !yes, !no, !veto, !anarchy or !democracy here does. Only you see the answer."
)
OFFLINE = "⚫ **Offline.** The machine's controls are restarting or stopped. The buttons come back with them."
PAUSED = "⏸️ **Paused by a moderator.** Commands and votes wait until the stream is back."
NO_VOTE = "No vote running. Commands such as `!rewind` or `!reboot` start one (see #rules)."
VOTES_OFF = "Power votes are off in this ruleset."
ENDED = {"done": "done", "error": "didn't work", "failed": "failed", "vetoed": "vetoed", "cancelled": "ended"}
ORANGE, BLUE, RED, GREY = 0xE67E22, 0x3498DB, 0xE74C3C, 0x95A5A6
_MARKDOWN = re.compile(r"([\\*_~`|<\[\]])")  # what turns names into bold, links, mentions…


def plain(text: str) -> str:
    """Chat's words (a name, a save's name) shown as written, not as Discord markdown."""
    return _MARKDOWN.sub(r"\\\1", text)


@dataclass(frozen=True)
class Button:
    word: str  # a press sends !word
    label: str
    style: discord.ButtonStyle
    emoji: str


BUTTONS = (
    Button("yes", "Yes", discord.ButtonStyle.success, "✅"),
    Button("no", "No", discord.ButtonStyle.danger, "❌"),
    Button("veto", "Veto", discord.ButtonStyle.secondary, "✋"),
    Button("anarchy", "Anarchy", discord.ButtonStyle.secondary, "🌀"),
    Button("democracy", "Democracy", discord.ButtonStyle.primary, "🗳️"),
)
WORDS = frozenset(button.word for button in BUTTONS)


@dataclass(frozen=True)
class PanelState:
    """What brain is doing, for the panel (Brain.panel_state)."""

    mode: str  # anarchy, democracy
    meter: float  # 0 to 100
    democracy_at: float
    anarchy_at: float
    locked_until: float  # Unix time: the mode stays put until then
    window_s: float  # democracy's ballot window
    vote: dict[str, Any] | None  # VoteEngine.view
    vote_ends: float | None  # Unix time the voting or veto window ends
    democracy: bool  # the ruleset has democracy (the meter, the mode buttons)
    votes: bool  # the ruleset has power votes
    paused: bool  # a panic or a content hold


@dataclass(frozen=True)
class Rendered:
    text: str
    colour: int
    enabled: frozenset[str]  # the buttons not greyed out


def render(state: PanelState | None, now: float) -> Rendered:
    """The panel for brain's state (None: brain is offline) at Unix time `now`."""
    if state is None:
        return Rendered(OFFLINE, GREY, frozenset())
    parts = [PAUSED] if state.paused else []
    if state.democracy:
        parts.append(_mode(state, now))
    parts.append(_vote(state) if state.votes else VOTES_OFF)
    enabled: set[str] = set()
    phase = state.vote["phase"] if state.vote else None
    if state.votes and not state.paused and phase == "voting":
        enabled |= {"yes", "no"}
    if state.votes and not state.paused and phase == "veto":
        enabled.add("veto")
    if state.democracy:  # the meter moves during a panic too, as when typed
        enabled |= {"anarchy", "democracy"}
    colour = RED if state.paused else BLUE if state.democracy and state.mode == "democracy" else ORANGE
    return Rendered("\n\n".join(parts), colour, frozenset(enabled))


def _mode(state: PanelState, now: float) -> str:
    if state.mode == "democracy":
        head = f"**Mode: democracy.** Commands are ballots: every {state.window_s:g} s, the most voted runs."
    else:
        head = "**Mode: anarchy.** Every command runs, in the order it comes."
    cells = max(0, min(20, round(state.meter / 5)))
    meter = f"`anarchy {'█' * cells}{'░' * (20 - cells)} democracy` {round(state.meter)}"
    rule = f"Democracy at {state.democracy_at:g}, anarchy at {state.anarchy_at:g}."
    if state.locked_until > now:
        rule += f" The mode can change again <t:{math.ceil(state.locked_until)}:R>."
    return f"{head}\n{meter}\n{rule}"


def _vote(state: PanelState) -> str:
    vote = state.vote
    if vote is None:
        return NO_VOTE
    what, by = plain(vote["text"]), plain(vote["by"])
    ends = f"<t:{math.ceil(state.vote_ends)}:R>" if state.vote_ends else "soon"
    match vote["phase"]:
        case "voting":
            count = f"✅ {vote['yes']} yes · ❌ {vote['no']} no"
            needs = f"needs {vote['quorum']} yes and {round(vote['threshold'] * 100)}%"
            return f"**Vote: {what}**, by {by}. Ends {ends}.\n{count} · {needs}."
        case "veto":
            vetoes = f"{vote['vetoes']} of {vote['veto_needed']} vetoes"
            return f"**Passed: {what}**, by {by}. It happens {ends} unless chat vetoes it ({vetoes})."
        case "running":
            return f"**{what}** passed: happening now."
        case phase:
            result = f": {plain(vote['result'])}" if vote["result"] else ""
            return f"**{what}**: {ENDED.get(phase, phase)}{result}."


def embed(shown: Rendered) -> discord.Embed:
    return discord.Embed(title=TITLE, description=shown.text, colour=shown.colour).set_footer(text=FOOTER)


Press = Callable[[discord.Interaction, str], Awaitable[None]]


class _PanelButton(discord.ui.Button["PanelView"]):
    def __init__(self, button: Button, enabled: bool, on_press: Press) -> None:
        super().__init__(
            style=button.style,
            label=button.label,
            emoji=button.emoji,
            custom_id=PREFIX + button.word,
            disabled=not enabled,
        )
        self.word = button.word
        self.on_press = on_press

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.on_press(interaction, self.word)


class PanelView(discord.ui.View):
    """The buttons: the same IDs on every panel, so a press works on any of them."""

    def __init__(self, on_press: Press, enabled: Iterable[str] = WORDS) -> None:
        super().__init__(timeout=None)
        on = frozenset(enabled)
        for button in BUTTONS:
            self.add_item(_PanelButton(button, button.word in on, on_press))


def is_panel(message: Any) -> bool:
    """A message with the panel's buttons (discord.Message)."""
    return any(
        str(getattr(child, "custom_id", None) or "").startswith(PREFIX)
        for row in getattr(message, "components", ())
        for child in getattr(row, "children", ())
    )


class Panel:
    """Keeps the one panel message in a channel: posts, edits and moves it."""

    def __init__(
        self,
        state: Callable[[], PanelState | None],
        on_press: Press,
        ready: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self.on_press = on_press
        self.ready = ready  # connected to Discord: not between a client and the next
        self.clock, self.wall = clock, wall
        self.channel: Any = None  # the discord.TextChannel it lives in
        self.me = 0  # the bot's user ID: its old panels are found by it
        self.message: Any = None  # the discord.Message showing it
        self.shown: Rendered | None = None
        self.under = 0  # messages posted under it
        self.posted = 0.0  # clock() when it was posted
        self.problem = ""  # the last error, until it works again
        self._cleaned = False
        self._task: asyncio.Task[None] | None = None

    def attach(self, channel: Any, me: int) -> None:
        """Discord is ready (again, after a reconnect): keep the panel in this channel.

        After a restart of the client, the message is reached through the new
        connection (a partial message: its ID is all it takes to edit it)."""
        if self.message is not None:
            same = self.channel is not None and channel.id == self.channel.id
            self.message = channel.get_partial_message(self.message.id) if same else None
        self.channel, self.me = channel, me
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def saw(self, channel_id: int, author_id: int) -> None:
        """A message was posted somewhere: under the panel, it buries it a little more."""
        if self.channel is not None and channel_id == self.channel.id and author_id != self.me:
            self.under += 1

    def deleted(self, message_id: int) -> None:
        """A message was deleted: if it was the panel (a moderator?), it's posted again."""
        if self.message is not None and self.message.id == message_id:
            self.message = None

    def status(self) -> str:
        if self.problem:
            return self.problem
        if self.message is not None:
            return "shown"
        return "posting" if self.channel is not None else "waiting for Discord"

    async def _run(self) -> None:
        while True:
            await self.update()
            await asyncio.sleep(EVERY)

    async def update(self) -> None:
        """One look: post, move or edit the panel if it needs it. Errors are logged when they change."""
        try:
            await self._update()
        except discord.NotFound:
            self.message = None  # deleted by someone: posted again next time
            self._trouble("the panel was deleted: posting it again")
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            self._trouble(f"can't post or edit the panel: {err or type(err).__name__}")
        except Exception as err:  # a bug: the whole story, once
            self._trouble(f"the panel failed: {type(err).__name__}", exc_info=True)
        else:
            self._trouble("")

    def _trouble(self, problem: str, exc_info: bool = False) -> None:
        if problem and problem != self.problem:
            log.warning("Discord: %s", problem, exc_info=exc_info)
        elif not problem and self.problem:
            log.info("Discord: the panel works again")
        self.problem = problem

    async def _update(self) -> None:
        if self.channel is None or not self.ready():
            return
        if not self._cleaned:
            self._cleaned = True  # once: without history access, the panel still goes up next time
            await self._delete_old()
        shown = render(self.state(), self.wall())
        if self.message is None:
            await self._post(shown)
        elif self.under >= BURIED and self.clock() - self.posted >= MOVE_GAP:
            old = self.message
            await self._post(shown)
            with contextlib.suppress(discord.NotFound):
                await old.delete()
        elif shown != self.shown:
            await self.message.edit(embed=embed(shown), view=PanelView(self.on_press, shown.enabled))
            self.shown = shown

    async def _post(self, shown: Rendered) -> None:
        self.message = await self.channel.send(
            embed=embed(shown), view=PanelView(self.on_press, shown.enabled)
        )
        self.shown, self.under, self.posted = shown, 0, self.clock()

    async def _delete_old(self) -> None:
        """Panels an earlier run left: it may have stopped without saying so."""
        async for message in self.channel.history(limit=HISTORY):
            if message.author.id == self.me and is_panel(message):
                with contextlib.suppress(discord.NotFound):
                    await message.delete()

    async def close(self) -> None:
        """A clean stop: the panel says brain is offline, its buttons greyed out."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.message is None or not self.ready():
            return
        offline = render(None, 0)
        with contextlib.suppress(discord.HTTPException, OSError, aiohttp.ClientError, TimeoutError):
            await asyncio.wait_for(
                self.message.edit(embed=embed(offline), view=PanelView(self.on_press, offline.enabled)),
                CLOSE_TIMEOUT,
            )
