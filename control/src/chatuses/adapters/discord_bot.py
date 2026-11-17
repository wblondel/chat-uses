"""Discord in (PLAN.md D9, Phase 4): `!` commands in #control, /status, and
the moderators' slash commands and alerts (Phase 5, PLAN.md 7.5-7.6).

The bot reads messages in one channel of one server (DISCORD_GUILD_ID,
DISCORD_CHANNEL: a channel name or ID). It needs the Message Content intent
(Developer Portal → Bot), which docs/phase0.md turns on. Server roles map
onto the unified identity: Owner, Moderator and Regular (the roles
infra/discord/setup_server.py creates), plus whoever owns the server.

Like YouTube, feedback goes on the overlay, not as replies: replies would
flood #control (D8). /status answers privately (ephemeral).

The control panel (discord_panel.py): one message in #control with Yes,
No, Veto, Anarchy and Democracy buttons, kept up to date. A press is the
presser typing that command in #control; only they see the answer.

Screen OCR alerts in #mod-alerts carry *Real* and *False alarm* buttons:
a moderator's verdict on each hit is what decides when enforcing is safe
(Phase 7). The latest press counts, and the post says whose it is.

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
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import discord
from discord import app_commands

from chatuses.adapters.discord_panel import Panel, PanelState, PanelView
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
HELP_GAP = 20.0  # seconds: !help in #control is answered once in this time
NOT_A_MODERATOR = "Only moderators can do that."

Deliver = Callable[[ChatMessage], None]
NOT_HERE = "This panel only works in the stream's server."
VERDICT_LINE = "Verdict: "


class OcrVerdict(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=r"chatuses:ocr:(?P<hit>[0-9]{1,12}):(?P<verdict>real|false)",
):
    """A screen OCR hit's buttons: the hit's ID is in the button's, so a press works after a restart."""

    def __init__(self, hit: int, verdict: str) -> None:
        real = verdict == "real"
        super().__init__(
            discord.ui.Button(
                label="Real" if real else "False alarm",
                style=discord.ButtonStyle.danger if real else discord.ButtonStyle.secondary,
                custom_id=f"chatuses:ocr:{hit}:{verdict}",
            )
        )
        self.hit, self.verdict = hit, verdict

    @classmethod
    async def from_custom_id(  # pyright: ignore[reportIncompatibleMethodOverride]
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: "re.Match[str]", /
    ) -> "OcrVerdict":
        return cls(int(match["hit"]), match["verdict"])

    async def callback(self, interaction: discord.Interaction) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        adapter = getattr(interaction.client, "adapter", None)
        if isinstance(adapter, DiscordChat):
            await adapter.verdict_pressed(interaction, self.hit, self.verdict)


def verdict_view(hit: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OcrVerdict(hit, "real"))
    view.add_item(OcrVerdict(hit, "false"))
    return view


def with_verdict(content: str, verdict: str, by: str) -> str:
    """The alert's text with its verdict line (replacing an earlier one)."""
    kept = [line for line in content.split("\n") if not line.startswith(VERDICT_LINE)]
    what = "✅ real" if verdict == "real" else "❎ false alarm"
    return "\n".join([*kept, f"{VERDICT_LINE}{what} ({by})"])[:2000]


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

        if adapter.me is not None:
            me_text = adapter.me

            @self.tree.command(name="me", description="Your points and rank (only you see the answer)")
            async def me(interaction: discord.Interaction) -> None:  # pyright: ignore[reportUnusedFunction]
                answer = me_text(user_id("dc", str(interaction.user.id)))
                await interaction.response.send_message(answer, ephemeral=True)

        if adapter.moderation is not None:
            self._moderator_commands(adapter.moderation)
        self.tree.copy_global_to(guild=guild)
        self._guild = guild

    def _moderator_commands(self, mod: "Moderation") -> None:
        """Moderators' tools: /panic, /resume, /ban, /timeout, /unban, /lockdown (PLAN.md 7.5,
        7.6); /endvote, /rewind-to, /mode (6.1)."""
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

        @tree.command(name="mode", description="Anarchy or democracy, now (whatever the meter says)")
        @hidden
        async def mode(  # pyright: ignore[reportUnusedFunction]
            interaction: discord.Interaction, mode: Literal["anarchy", "democracy"]
        ) -> None:
            await run(interaction, lambda by: mod.mode(by, mode))

        @tree.command(name="lockdown", description="Raid? Only regulars may send commands")
        @hidden
        async def lockdown(  # pyright: ignore[reportUnusedFunction]
            interaction: discord.Interaction, state: Literal["on", "off"]
        ) -> None:
            await run(interaction, lambda by: mod.lockdown(by, state == "on"))

    async def setup_hook(self) -> None:
        if self.adapter.panel is not None:  # presses on any panel, from the start
            self.add_view(PanelView(self.adapter.pressed))
        if self.adapter.moderation is not None:  # verdicts on screen OCR hits, old posts included
            self.add_dynamic_items(OcrVerdict)
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
        found = [c for c in guild.text_channels if self.adapter.channel in (c.name, str(c.id))]
        if not found:
            self.adapter.state, self.adapter.reason = (
                "connected",
                f"no channel {self.adapter.channel!r} in {guild.name}",
            )
            return
        self.adapter.state, self.adapter.reason = "connected", ""
        log.info("Discord: reading #%s in %s", self.adapter.channel, guild.name)
        self.adapter.flush_alerts()
        if self.adapter.panel is not None and self.user is not None:
            self.adapter.panel.attach(found[0], self.user.id)

    async def on_disconnect(self) -> None:
        if self.adapter.state == "connected":
            self.adapter.state = "reconnecting"

    async def on_resumed(self) -> None:
        self.adapter.state = "connected"

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self.adapter.panel is not None:
            self.adapter.panel.deleted(payload.message_id)

    async def on_message(self, message: discord.Message) -> None:
        author = message.author
        if self.adapter.panel is not None:
            self.adapter.panel.saw(message.channel.id, author.id)
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
            if chat.text.strip().casefold() == "!help":
                await self.adapter.answer_help(message)


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
        press: Callable[[ChatMessage], str] | None = None,
        panel_state: Callable[[], PanelState | None] | None = None,
        me: Callable[[str], str] | None = None,
        help_text: Callable[[], str] | None = None,
    ) -> None:
        self.token = token
        self.guild_id = guild_id
        self.channel = channel.removeprefix("#")
        self.deliver = deliver
        self.status_text = status_text
        self.moderation = moderation
        self.alerts_channel = alerts_channel.removeprefix("#")
        self.press = press  # a panel button: brain handles it as typed, returns the answer
        self.me = me  # /me: a user's points and rank (brain's me_text)
        self.help_text = help_text  # !help in #control: brain's list of commands
        self._helped = 0.0  # monotonic: when !help was last answered (once per HELP_GAP)
        self.panel = Panel(panel_state, self.pressed, self._ready) if press and panel_state else None
        self.presses = 0
        self.pinged: list[tuple[int, str]] = []  # (member ID, text): the driver's seat's pings
        self.state = "off"  # off, connecting, connected, reconnecting, stopped
        self.reason = ""
        self.messages = 0
        self._task: asyncio.Task[None] | None = None
        self._client: _Client | None = None
        self._alerts: deque[tuple[str, bytes | None, str, int | None]] = deque(maxlen=MAX_PENDING_ALERTS)
        self._sending: set[asyncio.Task[None]] = set()

    def _ready(self) -> bool:
        return self._client is not None and self._client.is_ready()

    async def pressed(self, interaction: discord.Interaction, word: str) -> None:
        """A panel button: the presser typing !word in #control. Only they see the answer."""
        user, guild = interaction.user, interaction.guild
        member = user if isinstance(user, discord.Member) else None
        chat = chat_from(
            guild_id=self.guild_id,
            channel=self.channel,
            message_guild_id=guild.id if guild else None,
            message_channel_id=interaction.channel_id or 0,
            message_channel_name=getattr(interaction.channel, "name", None),
            author_id=user.id,
            author_name=user.display_name,
            author_is_bot=user.bot,
            author_role_names=[role.name for role in member.roles] if member else [],
            author_owns_guild=guild is not None and guild.owner_id == user.id,
            content=f"!{word}",
            ts=time.time(),
        )
        if chat is None or self.press is None:
            answer = NOT_HERE
        else:
            self.presses += 1
            answer = self.press(chat)
        try:
            await interaction.response.send_message(answer, ephemeral=True)
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:  # too late (3 s), or offline
            log.warning("Discord: couldn't answer a panel press: %s", err)

    def alert(
        self,
        text: str,
        image: bytes | None = None,
        channel: str | None = None,
        verdict_for: int | None = None,
    ) -> None:
        """Post in #mod-alerts, or another channel such as #ops (with a PNG, as a spoiler);
        verdict_for: a screen OCR hit's ID, for its Real / False alarm buttons.

        Kept for later if Discord isn't reachable."""
        where = (channel or self.alerts_channel).removeprefix("#")
        self._alerts.append((text[:1900], image, where, verdict_for))
        self.flush_alerts()

    def ping(self, member_id: int, text: str) -> None:
        """Mention one member in the commands channel (the driver's seat's offer). Only them:
        no @everyone, no roles. Now or never: an offer that waits is no offer."""
        self.pinged.append((member_id, text[:1800]))
        client = self._client
        if client is None or not client.is_ready():
            return
        guild = client.get_guild(self.guild_id)
        channel = (
            next((c for c in guild.text_channels if self.channel in (c.name, str(c.id))), None)
            if guild
            else None
        )
        if channel is None:
            return
        mention = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=member_id)])
        task = asyncio.create_task(self._send_ping(channel, f"<@{member_id}> {text[:1800]}", mention))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _send_ping(
        self, channel: discord.TextChannel, text: str, mention: discord.AllowedMentions
    ) -> None:
        try:
            await channel.send(text, allowed_mentions=mention)
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            log.warning("Discord: couldn't ping in #%s: %s", channel.name, err)

    def pending_alerts(self) -> list[str]:
        """Alerts not posted yet (Discord unreachable, or no alerts channel)."""
        return [text for text, _image, _channel, _hit in self._alerts]

    async def verdict_pressed(self, interaction: discord.Interaction, hit: int, verdict: str) -> None:
        """Real or False alarm under a screen OCR alert: moderators only."""
        member, guild = interaction.user, interaction.guild
        roles = [r.name for r in member.roles] if isinstance(member, discord.Member) else []
        owns = guild is not None and guild.owner_id == member.id
        if (
            self.moderation is None
            or guild is None
            or guild.id != self.guild_id
            or not is_moderator(roles, owns)
        ):
            await interaction.response.send_message(NOT_A_MODERATOR, ephemeral=True)
            return
        answer = self.moderation.verdict(moderator_label(member.display_name, member.id), hit, verdict)
        try:
            if answer.ok and interaction.message is not None:
                content = with_verdict(interaction.message.content, verdict, member.display_name)
                await interaction.response.edit_message(
                    content=content, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await interaction.response.send_message(answer.text, ephemeral=True)
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            log.warning("Discord: couldn't answer a verdict: %s", err)

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
        kept: deque[tuple[str, bytes | None, str, int | None]] = deque(maxlen=MAX_PENDING_ALERTS)
        while self._alerts:
            text, image, name, hit = self._alerts.popleft()
            channel = channels.get(name)
            if channel is None:
                log.warning("Discord: no #%s channel for alerts", name)
                kept.append((text, image, name, hit))
                continue
            task = asyncio.create_task(self._post(channel, text, image, hit))
            self._sending.add(task)
            task.add_done_callback(self._sending.discard)
        self._alerts = kept

    async def _post(
        self, channel: discord.TextChannel, text: str, image: bytes | None = None, hit: int | None = None
    ) -> None:
        files = [discord.File(io.BytesIO(image), filename="screen.png", spoiler=True)] if image else []
        extra: dict[str, Any] = {"view": verdict_view(hit)} if hit is not None else {}
        try:
            await channel.send(text, files=files, allowed_mentions=discord.AllowedMentions.none(), **extra)
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            log.warning("Discord: couldn't post in #%s: %s", channel.name, err)

    async def answer_help(self, message: discord.Message) -> None:
        """!help in #control: the commands, as a reply (on stream the overlay shows them). Once
        per HELP_GAP, so it can't be used to flood the channel."""
        now = time.monotonic()
        if self.help_text is None or now - self._helped < HELP_GAP:
            return
        self._helped = now
        try:
            await message.reply(
                self.help_text(), mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            log.warning("Discord: couldn't answer !help: %s", err)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self.panel is not None:  # first, while the bot is still connected
            await self.panel.close()
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
            "presses": self.presses,
            "panel": self.panel.status() if self.panel is not None else "off",
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
