import asyncio
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from chatuses.adapters import discord_bot
from chatuses.adapters.discord_bot import DiscordChat, chat_from
from chatuses.pipeline import ChatMessage

GUILD = 111
CHANNEL_ID = 222


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def message(**changes: Any) -> ChatMessage | None:
    fields: dict[str, Any] = {
        "guild_id": GUILD,
        "channel": "control",
        "message_guild_id": GUILD,
        "message_channel_id": CHANNEL_ID,
        "message_channel_name": "control",
        "author_id": 42,
        "author_name": "Alice",
        "author_is_bot": False,
        "author_role_names": ["@everyone"],
        "author_owns_guild": False,
        "content": "!type hi",
        "ts": 5.0,
    }
    fields.update(changes)
    return chat_from(**fields)


def test_a_command_in_control_becomes_a_chat_message() -> None:
    assert message() == ChatMessage("dc:42", "Alice", "!type hi", 5.0, frozenset())


@pytest.mark.parametrize(
    "changes",
    [
        {"author_is_bot": True},  # bots, including ours
        {"message_guild_id": 999},  # another server
        {"message_guild_id": None},  # a DM
        {"message_channel_name": "general", "message_channel_id": 333},  # another channel
        {"content": ""},  # an image or sticker only
    ],
)
def test_messages_that_are_not_for_brain(changes: dict[str, Any]) -> None:
    assert message(**changes) is None


def test_the_channel_can_be_named_by_id() -> None:
    assert message(channel=str(CHANNEL_ID), message_channel_name="renamed") is not None


def test_roles_map_onto_the_identity() -> None:
    chat = message(author_role_names=["@everyone", "Moderator", "Regular", "Cool kids"])
    assert chat is not None and chat.roles == frozenset({"moderator", "regular"})
    chat = message(author_owns_guild=True)
    assert chat is not None and chat.roles == frozenset({"owner"})


def adapter(got: list[ChatMessage], channel: str = "#control") -> DiscordChat:
    return DiscordChat("token", GUILD, channel, got.append, lambda: "all good")


def test_on_message_feeds_brain() -> None:
    async def test() -> None:
        got: list[ChatMessage] = []
        chat = adapter(got)
        assert chat.channel == "control"  # "#control" works too
        client = discord_bot._Client(chat)  # pyright: ignore[reportPrivateUsage]
        fake = SimpleNamespace(
            author=SimpleNamespace(id=42, display_name="Alice", bot=False),
            guild=SimpleNamespace(id=GUILD, owner_id=42),
            channel=SimpleNamespace(id=CHANNEL_ID, name="control"),
            content="!enter",
        )
        await client.on_message(cast(discord.Message, fake))
        assert [(m.user_id, m.text, m.roles) for m in got] == [("dc:42", "!enter", frozenset({"owner"}))]
        assert chat.status()["messages"] == 1
        await client.close()

    run(test)


def test_a_bad_token_or_missing_intent_stops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        for error, reason in [
            (discord.LoginFailure("nope"), "token"),
            (discord.PrivilegedIntentsRequired(None), "Message Content"),
        ]:

            async def start(
                _self: discord.Client, _token: str, *, reconnect: bool = True, e: Exception = error
            ) -> None:
                raise e

            monkeypatch.setattr(discord.Client, "start", start)
            chat = adapter([])
            chat.start()
            async with asyncio.timeout(2):
                while chat.state != "stopped":
                    await asyncio.sleep(0.01)
            assert reason in chat.reason
            await chat.close()

    run(test)


def test_unexpected_failures_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discord_bot, "RETRY", (0.01, 0.02))
    attempts: list[int] = []

    async def start(_self: discord.Client, _token: str, *, reconnect: bool = True) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("network down")
        raise discord.LoginFailure("now the token is wrong")

    monkeypatch.setattr(discord.Client, "start", start)

    async def test() -> None:
        chat = adapter([])
        chat.start()
        async with asyncio.timeout(2):
            while chat.state != "stopped":
                await asyncio.sleep(0.01)
        assert len(attempts) == 3
        await chat.close()

    run(test)
