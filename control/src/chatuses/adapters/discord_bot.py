"""Discord in (PLAN.md D9, Phase 4): `!` commands in #control, /status, and
the moderators' slash commands and alerts (Phase 5, PLAN.md 7.5-7.6).

The bot reads messages in one channel of one server (DISCORD_GUILD_ID,
DISCORD_CHANNEL: a channel name or ID). It needs the Message Content intent
(Developer Portal → Bot), which docs/phase0.md turns on. Server roles map
onto the unified identity: Owner, Moderator and Regular (the roles
infra/discord/setup_server.py creates), plus whoever owns the server.

Like YouTube, feedback goes on the overlay, not as replies: replies would
flood #control (D8). /status answers privately (ephemeral).

Moderators (the Moderator or Owner role, or the server's owner) get /panic,
/resume, /ban, /timeout, /unban and /lockdown. Discord shows them only to
members who may time people out (the Moderator role can); brain checks the
role again on every use. Answers are private; what was done is posted in
#mod-alerts (DISCORD_ALERTS_CHANNEL), with mentions turned off.

discord.py reconnects by itself through network trouble; a wrong token or a
missing intent stop the adapter with the reason in its status.
"""

import asyncio
import contextlib
import io
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import discord
from discord import app_commands

from chatuses.identity import user_id
from chatuses.pipeline import ChatMessage

if TYPE_CHECKING:
    from chatuses.safety.moderation import Answer, Moderation

log = logging.getLogger(__name__)


class _NoVoiceWarnings(logging.Filter):
    """discord.py warns at every start that its voice libraries are missing: no voice until Phase 11."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


logging.getLogger("discord.client").addFilter(_NoVoiceWarnings())

ROLE_NAMES = {"Owner": "owner", "Moderator": "moderator", "Regular": "regular"}
MODERATOR_ROLE_NAMES = frozenset({"Owner", "Moderator"})
RETRY = (5.0, 300.0)  # seconds between restarts after an unexpected failure
MAX_PENDING_ALERTS = 20  # kept while Discord is unreachable; older ones are dropped
NOT_A_MODERATOR = "Only moderators can do that."

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


def is_moderator(role_names: Iterable[str], owns_guild: bool) -> bool:
    return owns_guild or bool(MODERATOR_ROLE_NAMES & set(role_names))


def moderator_label(name: str, discord_id: int) -> str:
    """Who did it, for the event log and #mod-alerts."""
    return f"{name} (dc:{discord_id}) on Discord"


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

        if adapter.moderation is not None:
            self._moderator_commands(adapter.moderation)
        self.tree.copy_global_to(guild=guild)
        self._guild = guild

    def _moderator_commands(self, mod: "Moderation") -> None:
        """Moderators' tools: /panic, /resume, /ban, /timeout, /unban, /lockdown (PLAN.md 7.5,
        7.6); /endvote, /rewind-to (6.1)."""
        tree = self.tree
        hidden = app_commands.default_permissions(moderate_members=True)  # shown to moderators only

        async def run(interaction: discord.Interaction, act: Callable[[str], "Answer"]) -> None:
            member = interaction.user
            guild = interaction.guild
            roles = [r.name for r in member.roles] if isinstance(member, discord.Member) else []
            owns = guild is not None and guild.owner_id == member.id
            if guild is None or guild.id != self.adapter.guild_id or not is_moderator(roles, owns):
                await interaction.response.send_message(NOT_A_MODERATOR, ephemeral=True)
                return
            answer = act(moderator_label(member.display_name, member.id))
            await interaction.response.send_message(answer.text, ephemeral=True)

        @tree.command(name="panic", description="Cover the stream, mute the machine, freeze input")
        @app_commands.describe(reason="What's on screen (for #mod-alerts)")
        @hidden
        async def panic(interaction: discord.Interaction, reason: str = "") -> None:  # pyright: ignore[reportUnusedFunction]
            await run(interaction, lambda by: mod.panic(by, reason))

        @tree.command(name="resume", description="End the panic: the stream goes live again")
        @hidden
        async def resume(interaction: discord.Interaction) -> None:  # pyright: ignore[reportUnusedFunction]
            await run(interaction, lambda by: mod.resume(by))

        @tree.command(name="ban", description="Stop someone's commands on every platform")
        @app_commands.describe(
            who="Their name as the overlay shows it, or their ID (yt:…, dc:…)",
            duration="30m, 2h, 7d, 1w, or perm",
            reason="For the log",
        )
        @hidden
        async def ban(  # pyright: ignore[reportUnusedFunction]
            interaction: discord.Interaction, who: str, duration: str, reason: str = ""
        ) -> None:
            await run(interaction, lambda by: mod.ban(by, who, duration, reason))

        @tree.command(name="timeout", description="Stop someone's commands for a while (10 minutes)")
        @app_commands.describe(who="Their name as the overlay shows it, or their ID", minutes="1 to 10080")
        @hidden
        async def timeout(  # pyright: ignore[reportUnusedFunction]
            interaction: discord.Interaction, who: str, minutes: app_commands.Range[int, 1, 10080] = 10
        ) -> None:
            await run(interaction, lambda by: mod.timeout(by, who, minutes))

        @tree.command(name="unban", description="Lift someone's ban or timeout")
        @app_commands.describe(who="Their name as the overlay shows it, or their ID")
        @hidden
        async def unban(interaction: discord.Interaction, who: str) -> None:  # pyright: ignore[reportUnusedFunction]
            await run(interaction, lambda by: mod.unban(by, who))

        @tree.command(name="endvote", description="End the power vote running now")
        @hidden
        async def endvote(interaction: discord.Interaction) -> None:  # pyright: ignore[reportUnusedFunction]
            await run(interaction, lambda by: mod.endvote(by))

        @tree.command(name="rewind-to", description="Put the machine back to a snapshot now (no vote)")
        @app_commands.describe(snapshot="Its number (the dashboard lists them) or a save's name")
        @hidden
        async def rewind_to(interaction: discord.Interaction, snapshot: str) -> None:  # pyright: ignore[reportUnusedFunction]
            await run(interaction, lambda by: mod.rewind_to(by, snapshot))

        @tree.command(name="lockdown", description="Raid? Only regulars may send commands")
        @hidden
        async def lockdown(  # pyright: ignore[reportUnusedFunction]
            interaction: discord.Interaction, state: Literal["on", "off"]
        ) -> None:
            await run(interaction, lambda by: mod.lockdown(by, state == "on"))

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
        self.adapter.flush_alerts()

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
        self,
        token: str,
        guild_id: int,
        channel: str,
        deliver: Deliver,
        status_text: Callable[[], str],
        moderation: "Moderation | None" = None,
        alerts_channel: str = "mod-alerts",
    ) -> None:
        self.token = token
        self.guild_id = guild_id
        self.channel = channel.removeprefix("#")
        self.deliver = deliver
        self.status_text = status_text
        self.moderation = moderation
        self.alerts_channel = alerts_channel.removeprefix("#")
        self.state = "off"  # off, connecting, connected, reconnecting, stopped
        self.reason = ""
        self.messages = 0
        self._task: asyncio.Task[None] | None = None
        self._client: _Client | None = None
        self._alerts: deque[tuple[str, bytes | None, str]] = deque(maxlen=MAX_PENDING_ALERTS)
        self._sending: set[asyncio.Task[None]] = set()

    def alert(self, text: str, image: bytes | None = None, channel: str | None = None) -> None:
        """Post in #mod-alerts, or another channel such as #ops (with a PNG, as a spoiler).

        Kept for later if Discord isn't reachable."""
        self._alerts.append((text[:1900], image, (channel or self.alerts_channel).removeprefix("#")))
        self.flush_alerts()

    def pending_alerts(self) -> list[str]:
        """Alerts not posted yet (Discord unreachable, or no alerts channel)."""
        return [text for text, _image, _channel in self._alerts]

    def flush_alerts(self) -> None:
        client = self._client
        if client is None or not client.is_ready():
            return
        guild = client.get_guild(self.guild_id)
        channels = (
            {c.name: c for c in guild.text_channels} | {str(c.id): c for c in guild.text_channels}
            if guild
            else {}
        )
        kept: deque[tuple[str, bytes | None, str]] = deque(maxlen=MAX_PENDING_ALERTS)
        while self._alerts:
            text, image, name = self._alerts.popleft()
            channel = channels.get(name)
            if channel is None:
                log.warning("Discord: no #%s channel for alerts", name)
                kept.append((text, image, name))
                continue
            task = asyncio.create_task(self._post(channel, text, image))
            self._sending.add(task)
            task.add_done_callback(self._sending.discard)
        self._alerts = kept

    async def _post(self, channel: discord.TextChannel, text: str, image: bytes | None = None) -> None:
        files = [discord.File(io.BytesIO(image), filename="screen.png", spoiler=True)] if image else []
        try:
            await channel.send(text, files=files, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            log.warning("Discord: couldn't post in #%s: %s", channel.name, err)

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
            "alerts_waiting": len(self._alerts),
        }

    async def _run(self) -> None:
        wait = RETRY[0]
        while True:
            client = _Client(self)
            self._client = client
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
