import asyncio
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord import app_commands

from chatuses.adapters import discord_bot
from chatuses.adapters.discord_bot import DiscordChat, chat_from
from chatuses.pipeline import ChatMessage
from chatuses.safety.moderation import Moderation
from chatuses.safety.strikes import Strikes
from chatuses.store import Store

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
        if len(attempts) == 1:
            raise OSError("network down")
        if len(attempts) == 2:
            raise RuntimeError("a bug")
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


def test_voice_library_warnings_are_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    logger = discord_bot.logging.getLogger("discord.client")
    with caplog.at_level("WARNING", logger="discord.client"):
        logger.warning("PyNaCl is not installed, voice will NOT be supported")
        logger.warning("something that matters")
    assert [r.getMessage() for r in caplog.records] == ["something that matters"]


# --- moderators (Phase 5 step 3) ----------------------------------------------


def test_who_is_a_moderator() -> None:
    assert discord_bot.is_moderator(["@everyone", "Moderator"], False)
    assert discord_bot.is_moderator(["Owner"], False)
    assert discord_bot.is_moderator([], True)  # whoever owns the server
    assert not discord_bot.is_moderator(["Regular", "moderator"], False)  # role names are exact


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send_message(self, text: str, *, ephemeral: bool = False) -> None:
        self.sent.append((text, ephemeral))


def interaction(user_id: int, guild_id: int = GUILD, owner_id: int = 1) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Mia"),
        guild=SimpleNamespace(id=guild_id, owner_id=owner_id),
        response=Recorder(),
    )


class NoEffects:
    def panic_changed(self, on: bool, kind: str = "panic") -> None: ...
    def lockdown_changed(self, on: bool) -> None: ...
    def alert(self, text: str) -> None: ...
    def snapshot(self, name: str) -> None: ...


def test_moderator_slash_commands_are_role_gated_and_private() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            mod = Moderation(store, NoEffects(), Strikes())
            chat = DiscordChat("token", GUILD, "control", lambda _m: None, lambda: "", mod)
            client = discord_bot._Client(chat)  # pyright: ignore[reportPrivateUsage]
            names = {c.name for c in client.tree.get_commands()}
            assert names >= {"status", "panic", "resume", "ban", "timeout", "unban", "lockdown"}
            panic = cast(app_commands.Command[Any, Any, None], client.tree.get_command("panic"))
            assert panic.default_permissions is not None and panic.default_permissions.moderate_members
            assert client.tree.get_command("status").default_permissions is None  # type: ignore[union-attr]

            viewer = interaction(7)
            await panic.callback(viewer, reason="x")  # pyright: ignore[reportCallIssue]
            assert viewer.response.sent == [(discord_bot.NOT_A_MODERATOR, True)] and not mod.panicking
            elsewhere = interaction(7, guild_id=999, owner_id=7)  # owns another server
            await panic.callback(elsewhere, reason="x")  # pyright: ignore[reportCallIssue]
            assert not mod.panicking

            owner = interaction(7, owner_id=7)
            await panic.callback(owner, reason="gore")  # pyright: ignore[reportCallIssue]
            assert mod.panic_state is not None and mod.panic_state["by"] == "Mia (dc:7) on Discord"
            assert owner.response.sent[0][1] is True  # only they see the answer
            ban = cast(app_commands.Command[Any, Any, None], client.tree.get_command("ban"))
            await ban.callback(owner, who="yt:troll", duration="1d")  # pyright: ignore[reportCallIssue]
            assert store.banned_until("yt:troll") > 0
            lockdown = cast(app_commands.Command[Any, Any, None], client.tree.get_command("lockdown"))
            await lockdown.callback(owner, state="on")  # pyright: ignore[reportCallIssue]
            assert mod.locked
            await client.close()

    run(test)


def test_alerts_wait_while_discord_is_unreachable() -> None:
    chat = DiscordChat("token", GUILD, "control", lambda _m: None, lambda: "")
    for i in range(discord_bot.MAX_PENDING_ALERTS + 5):
        chat.alert(f"alert {i}")
    pending = chat.pending_alerts()
    assert len(pending) == discord_bot.MAX_PENDING_ALERTS and pending[-1] == "alert 24"
    assert chat.status()["alerts_waiting"] == discord_bot.MAX_PENDING_ALERTS


def test_alerts_go_to_mod_alerts_without_mentions() -> None:
    async def test() -> None:
        posted: list[tuple[str, Any]] = []
        sent_files: list[list[Any]] = []

        async def send(text: str, *, files: list[Any], allowed_mentions: Any) -> None:
            posted.append((text, allowed_mentions))
            sent_files.append(files)

        chat = DiscordChat(
            "token", GUILD, "control", lambda _m: None, lambda: "", alerts_channel="#mod-alerts"
        )
        chat.alert("before connecting @everyone")
        channels = [
            SimpleNamespace(name="control", id=1),
            SimpleNamespace(name="mod-alerts", id=2, send=send),
        ]

        def get_guild(_id: int) -> Any:
            return SimpleNamespace(text_channels=channels)

        fake = SimpleNamespace(is_ready=lambda: True, get_guild=get_guild)
        chat._client = cast(Any, fake)  # pyright: ignore[reportPrivateUsage]
        chat.flush_alerts()
        chat.alert("after")
        await asyncio.sleep(0)
        assert [text for text, _ in posted] == ["before connecting @everyone", "after"]
        mentions = posted[0][1]
        assert not mentions.everyone and not mentions.users and not mentions.roles
        assert chat.pending_alerts() == []

    run(test)
