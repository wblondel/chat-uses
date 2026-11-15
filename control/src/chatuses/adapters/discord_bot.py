"""Discord in (PLAN.md D9, Phase 4): `!` commands in #control, and /status.

The bot reads messages in one channel of one server (DISCORD_GUILD_ID,
DISCORD_CHANNEL: a channel name or ID). It needs the Message Content intent
(Developer Portal → Bot), which docs/phase0.md turns on. Server roles map
onto the unified identity: Owner, Moderator and Regular (the roles
infra/discord/setup_server.py creates), plus whoever owns the server.

Like YouTube, feedback goes on the overlay, not as replies: replies would
flood #control (D8). /status answers privately (ephemeral).

discord.py reconnects by itself through network trouble; a wrong token or a
missing intent stop the adapter with the reason in its status.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

import aiohttp
import discord
from discord import app_commands

from chatuses.identity import user_id
from chatuses.pipeline import ChatMessage

log = logging.getLogger(__name__)


class _NoVoiceWarnings(logging.Filter):
    """discord.py warns at every start that its voice libraries are missing: no voice until Phase 11."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


logging.getLogger("discord.client").addFilter(_NoVoiceWarnings())

ROLE_NAMES = {"Owner": "owner", "Moderator": "moderator", "Regular": "regular"}
RETRY = (5.0, 300.0)  # seconds between restarts after an unexpected failure

Deliver = Callable[[ChatMessage], None]


def chat_from(
    *,
    guild_id: int,
    channel: str,
    message_guild_id: int | None,
    message_channel_id: int,
    message_channel_name: str | None,
    author_id: int,
    author_name: str,
    author_is_bot: bool,
    author_role_names: Iterable[str],
    author_owns_guild: bool,
    content: str,
    ts: float,
) -> ChatMessage | None:
    """A Discord message as a chat message for brain, or None if it isn't one."""
    if author_is_bot or message_guild_id != guild_id or not content:
        return None
    if channel not in (str(message_channel_id), message_channel_name):
        return None
    roles = {ROLE_NAMES[name] for name in author_role_names if name in ROLE_NAMES}
    if author_owns_guild:
        roles.add("owner")
    return ChatMessage(user_id("dc", str(author_id)), author_name, content, ts, frozenset(roles))


class _Client(discord.Client):
    def __init__(self, adapter: "DiscordChat") -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.adapter = adapter
        self.tree = app_commands.CommandTree(self)
        guild = discord.Object(id=adapter.guild_id)

        @self.tree.command(
            name="status", description="Is the machine taking commands? (only you see the answer)"
        )
        async def status(interaction: discord.Interaction) -> None:  # pyright: ignore[reportUnusedFunction]
            await interaction.response.send_message(adapter.status_text(), ephemeral=True)

        self.tree.copy_global_to(guild=guild)
        self._guild = guild

    async def setup_hook(self) -> None:
        # Guild commands show up at once (global ones can take an hour).
        await self.tree.sync(guild=self._guild)

    async def on_ready(self) -> None:
        guild = self.get_guild(self.adapter.guild_id)
        if guild is None:
            self.adapter.state, self.adapter.reason = (
                "connected",
                "the bot isn't in that server (DISCORD_GUILD_ID)",
            )
            return
        names = {c.name for c in guild.text_channels} | {str(c.id) for c in guild.text_channels}
        if self.adapter.channel not in names:
            self.adapter.state, self.adapter.reason = (
                "connected",
                f"no channel {self.adapter.channel!r} in {guild.name}",
            )
            return
        self.adapter.state, self.adapter.reason = "connected", ""
        log.info("Discord: reading #%s in %s", self.adapter.channel, guild.name)

    async def on_disconnect(self) -> None:
        if self.adapter.state == "connected":
            self.adapter.state = "reconnecting"

    async def on_resumed(self) -> None:
        self.adapter.state = "connected"

    async def on_message(self, message: discord.Message) -> None:
        author = message.author
        member = author if isinstance(author, discord.Member) else None
        guild = message.guild
        chat = chat_from(
            guild_id=self.adapter.guild_id,
            channel=self.adapter.channel,
            message_guild_id=guild.id if guild else None,
            message_channel_id=message.channel.id,
            message_channel_name=getattr(message.channel, "name", None),
            author_id=author.id,
            author_name=author.display_name,
            author_is_bot=author.bot,
            author_role_names=[role.name for role in member.roles] if member else [],
            author_owns_guild=guild is not None and guild.owner_id == author.id,
            content=message.content,
            ts=time.time(),
        )
        if chat is not None:
            self.adapter.messages += 1
            self.adapter.deliver(chat)


class DiscordChat:
    def __init__(
        self, token: str, guild_id: int, channel: str, deliver: Deliver, status_text: Callable[[], str]
    ) -> None:
        self.token = token
        self.guild_id = guild_id
        self.channel = channel.removeprefix("#")
        self.deliver = deliver
        self.status_text = status_text
        self.state = "off"  # off, connecting, connected, reconnecting, stopped
        self.reason = ""
        self.messages = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "channel": self.channel,
            "messages": self.messages,
        }

    async def _run(self) -> None:
        wait = RETRY[0]
        while True:
            client = _Client(self)
            self.state, self.reason = "connecting", ""
            try:
                await client.start(self.token)
            except discord.LoginFailure:
                self.state, self.reason = "stopped", "Discord refused the bot token"
                return
            except discord.PrivilegedIntentsRequired:
                self.state, self.reason = (
                    "stopped",
                    "turn on the Message Content intent (Developer Portal → Bot)",
                )
                return
            except (OSError, aiohttp.ClientError, discord.GatewayNotFound, discord.ConnectionClosed) as err:
                # Network trouble discord.py gave up on: one line, then retry, slowly.
                self.state, self.reason = "reconnecting", type(err).__name__
                log.warning("Discord: can't connect (%s), retrying in %.0f s", type(err).__name__, wait)
            except Exception as err:  # a bug: the whole story, then retry
                self.state, self.reason = "reconnecting", type(err).__name__
                log.exception("Discord: failed, retrying in %.0f s", wait)
            finally:
                if not client.is_closed():
                    await client.close()
            await asyncio.sleep(wait)
            wait = min(wait * 2, RETRY[1])
