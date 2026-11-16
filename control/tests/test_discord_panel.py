import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import discord

from chatuses.adapters import discord_panel
from chatuses.adapters.discord_panel import (
    BURIED,
    MOVE_GAP,
    OFFLINE,
    PREFIX,
    Panel,
    PanelState,
    PanelView,
    is_panel,
    render,
)

NOW = 1_800_000_000.0
STATE = PanelState(
    mode="anarchy",
    meter=50,
    democracy_at=80,
    anarchy_at=20,
    locked_until=0,
    window_s=12,
    vote=None,
    vote_ends=None,
    democracy=True,
    votes=True,
    paused=False,
)
VOTE: dict[str, Any] = {
    "id": 1,
    "text": "!rewind 2",
    "by": "**Ann**",
    "phase": "voting",
    "ends_in": 20.0,
    "yes": 3,
    "no": 1,
    "quorum": 5,
    "threshold": 0.66,
    "vetoes": 0,
    "veto_needed": 3,
    "result": "",
}


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def test_offline_everything_greyed_out() -> None:
    shown = render(None, NOW)
    assert shown.text == OFFLINE and shown.enabled == frozenset()


def test_anarchy_without_a_vote() -> None:
    shown = render(STATE, NOW)
    assert "**Mode: anarchy.**" in shown.text
    assert "`anarchy ██████████░░░░░░░░░░ democracy` 50" in shown.text
    assert "Democracy at 80, anarchy at 20." in shown.text and "<t:" not in shown.text
    assert discord_panel.NO_VOTE in shown.text
    assert shown.enabled == {"anarchy", "democracy"} and shown.colour == discord_panel.ORANGE


def test_democracy_locked_shows_when_it_can_change_as_a_discord_timestamp() -> None:
    shown = render(replace(STATE, mode="democracy", meter=81.4, locked_until=NOW + 99.5), NOW)
    assert "**Mode: democracy.** Commands are ballots: every 12 s, the most voted runs." in shown.text
    assert "████████████████░░░░ democracy` 81" in shown.text
    assert f"The mode can change again <t:{int(NOW) + 100}:R>." in shown.text
    assert shown.colour == discord_panel.BLUE


def test_a_vote_running_and_its_countdown() -> None:
    shown = render(replace(STATE, vote=VOTE, vote_ends=NOW + 20), NOW)
    assert (
        f"**Vote: !rewind 2**, by \\*\\*Ann\\*\\*. Ends <t:{int(NOW) + 20}:R>." in shown.text
    )  # no markdown
    assert "✅ 3 yes · ❌ 1 no · needs 5 yes and 66%." in shown.text
    assert shown.enabled == {"yes", "no", "anarchy", "democracy"}


def test_the_veto_countdown_and_the_end() -> None:
    veto = render(replace(STATE, vote={**VOTE, "phase": "veto", "vetoes": 1}, vote_ends=NOW + 30), NOW)
    assert "It happens <t:" in veto.text and "(1 of 3 vetoes)" in veto.text
    assert veto.enabled == {"veto", "anarchy", "democracy"}
    failed = {**VOTE, "phase": "failed", "result": "not enough votes (3 yes of 5 needed)"}
    shown = render(replace(STATE, vote=failed), NOW)
    assert "**!rewind 2**: failed: not enough votes (3 yes of 5 needed)." in shown.text
    assert shown.enabled == {"anarchy", "democracy"}
    assert "happening now" in render(replace(STATE, vote={**VOTE, "phase": "running"}), NOW).text


def test_a_panic_greys_out_the_vote() -> None:
    shown = render(replace(STATE, paused=True, vote=VOTE, vote_ends=NOW + 20), NOW)
    assert shown.text.startswith(discord_panel.PAUSED)
    assert shown.enabled == {"anarchy", "democracy"} and shown.colour == discord_panel.RED


def test_a_ruleset_without_democracy_or_power_votes() -> None:
    shown = render(replace(STATE, democracy=False, votes=False), NOW)
    assert shown.text == discord_panel.VOTES_OFF and shown.enabled == frozenset()


def test_the_buttons_and_a_press() -> None:
    async def test() -> None:
        pressed: list[str] = []

        async def on_press(_interaction: discord.Interaction, word: str) -> None:
            pressed.append(word)

        view = PanelView(on_press, {"yes"})
        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        assert [(b.label, b.custom_id, b.disabled) for b in buttons] == [
            ("Yes", PREFIX + "yes", False),
            ("No", PREFIX + "no", True),
            ("Veto", PREFIX + "veto", True),
            ("Anarchy", PREFIX + "anarchy", True),
            ("Democracy", PREFIX + "democracy", True),
        ]
        assert view.is_persistent()  # presses work across restarts
        await buttons[1].callback(SimpleNamespace())  # pyright: ignore[reportArgumentType]
        assert pressed == ["no"]

    run(test)


# --- keeping the message ------------------------------------------------------------

ME = 99


def row(*ids: str) -> Any:
    return SimpleNamespace(children=[SimpleNamespace(custom_id=i) for i in ids])


class FakeMessage:
    ids = 0

    def __init__(self, channel: "FakeChannel", author: int, embed: Any = None, view: Any = None) -> None:
        FakeMessage.ids += 1
        self.id = FakeMessage.ids
        self.channel, self.author = channel, SimpleNamespace(id=author)
        self.embed, self.view = embed, view
        self.components = [row(PREFIX + "yes")] if view else []
        self.edits = 0
        self.gone = False

    async def edit(self, *, embed: Any, view: Any) -> None:
        if self.gone:
            raise discord.NotFound(
                cast(Any, SimpleNamespace(status=404, reason="Not Found")), "Unknown Message"
            )
        self.embed, self.view = embed, view
        self.edits += 1

    async def delete(self) -> None:
        self.channel.messages.remove(self)
        self.gone = True

    def enabled(self) -> set[str]:
        return {b.custom_id.removeprefix(PREFIX) for b in self.view.children if not b.disabled}


class FakeChannel:
    id = 222

    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        self.refuse = False

    async def send(self, *, embed: Any, view: Any) -> FakeMessage:
        if self.refuse:
            response = cast(Any, SimpleNamespace(status=403, reason="Forbidden"))
            raise discord.Forbidden(response, {"code": 50013, "message": "Missing Permissions"})
        message = FakeMessage(self, ME, embed, view)
        self.messages.append(message)
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage:
        """Another connection's handle on the same message: here, the message itself."""
        return next(m for m in self.messages if m.id == message_id)

    async def history(self, *, limit: int) -> AsyncIterator[FakeMessage]:
        for message in reversed(self.messages[-limit:]):
            yield message


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def no_press(_interaction: discord.Interaction, _word: str) -> None: ...


def keeper(
    state: list[PanelState | None], ready: list[bool] | None = None
) -> tuple[Panel, FakeChannel, Clock]:
    clock, channel, up = Clock(), FakeChannel(), ready or [True]
    panel = Panel(lambda: state[0], no_press, lambda: up[0], clock=clock, wall=lambda: NOW)
    panel.channel, panel.me = channel, ME
    return panel, channel, clock


def test_old_panels_go_and_one_is_posted_then_edited_only_on_changes() -> None:
    async def test() -> None:
        state: list[PanelState | None] = [STATE]
        panel, channel, _clock = keeper(state)
        old = FakeMessage(channel, ME, view=True)  # an earlier run's
        someone = FakeMessage(channel, 5)
        someone.components = [row(PREFIX + "yes")]  # looks like one, but not the bot's
        mine = FakeMessage(channel, ME)  # the bot's, not a panel (an alert?)
        channel.messages = [old, someone, mine]
        await panel.update()
        assert channel.messages[:2] == [someone, mine] and len(channel.messages) == 3
        message = channel.messages[2]
        assert is_panel(message) and message.embed.title == discord_panel.TITLE
        assert message.enabled() == {"anarchy", "democracy"} and panel.status() == "shown"
        await panel.update()
        assert message.edits == 0  # nothing changed
        state[0] = replace(STATE, vote=VOTE, vote_ends=NOW + 20)
        await panel.update()
        assert message.edits == 1 and message.enabled() == {"yes", "no", "anarchy", "democracy"}
        assert "**Vote: !rewind 2**" in message.embed.description
        assert len(channel.messages) == 3  # still one panel

    run(test)


def test_a_buried_panel_moves_to_the_bottom_but_not_too_often() -> None:
    async def test() -> None:
        state: list[PanelState | None] = [STATE]
        panel, channel, clock = keeper(state)
        await panel.update()
        first = panel.message
        for _ in range(BURIED):
            panel.saw(channel.id, 5)
            panel.saw(channel.id, ME)  # the bot's own don't count
            panel.saw(333, 5)  # nor other channels'
        assert panel.under == BURIED
        clock.now += MOVE_GAP - 1
        await panel.update()
        assert panel.message is first  # not yet: it moved (was posted) less than a minute ago
        clock.now += 1
        await panel.update()
        assert panel.message is not first and first.gone
        assert channel.messages == [panel.message] and panel.under == 0

    run(test)


def test_a_deleted_panel_is_posted_again_and_errors_are_reported() -> None:
    async def test() -> None:
        state: list[PanelState | None] = [STATE]
        panel, channel, _clock = keeper(state)
        await panel.update()
        first = panel.message
        await first.delete()  # a moderator deleted it, and Discord didn't say (or not yet)
        state[0] = replace(STATE, meter=52)
        await panel.update()
        assert panel.message is None and "deleted" in panel.status()
        await panel.update()
        second: Any = getattr(panel, "message")  # noqa: B009 (pyright still thinks it's None)
        assert second is not None and second is not first and panel.status() == "shown"
        panel.deleted(12345)  # another message
        assert panel.message is second
        await second.delete()
        panel.deleted(second.id)  # Discord said so: posted again even with nothing new to show
        await panel.update()
        assert panel.message is not None and panel.message is not second
        channel.refuse, panel.message = True, None
        await panel.update()
        assert "Missing Permissions" in panel.status()

    run(test)


def test_without_history_access_the_panel_goes_up_all_the_same() -> None:
    async def test() -> None:
        panel, channel, _clock = keeper([STATE])

        def history(*, limit: int) -> AsyncIterator[FakeMessage]:
            response = cast(Any, SimpleNamespace(status=403, reason="Forbidden"))
            raise discord.Forbidden(response, {"code": 50001, "message": "Missing Access"})

        channel.history = history  # type: ignore[method-assign]
        await panel.update()
        assert "Missing Access" in panel.status() and panel.message is None
        await panel.update()
        assert panel.message is not None and panel.status() == "shown"

    run(test)


def test_a_clean_stop_says_offline() -> None:
    async def test() -> None:
        state: list[PanelState | None] = [replace(STATE, vote=VOTE, vote_ends=NOW + 20)]
        panel, channel, _clock = keeper(state)
        panel.channel = None
        panel.attach(channel, ME)  # Discord is ready: the loop starts
        async with asyncio.timeout(2):
            while panel.message is None:
                await asyncio.sleep(0.01)
        message = panel.message
        await panel.close()
        assert message.embed.description == OFFLINE and message.enabled() == set()

    run(test)


def test_between_connections_it_waits_and_then_reaches_the_same_message_anew() -> None:
    async def test() -> None:
        state: list[PanelState | None] = [STATE]
        ready = [True]
        panel, channel, _clock = keeper(state, ready)
        await panel.update()
        first = panel.message
        ready[0] = False  # the client failed; brain starts another
        state[0] = replace(STATE, meter=52)
        await panel.update()
        assert first.edits == 0 and len(channel.messages) == 1
        again = FakeChannel()  # the new client's channel object, same channel
        again.messages = channel.messages
        ready[0] = True
        panel.attach(again, ME)
        await panel.update()
        assert panel.message is first and first.edits == 1 and len(again.messages) == 1
        await panel.close()

    run(test)
