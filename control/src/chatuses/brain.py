"""brain: chat messages in, desktop input and overlay out (PLAN.md 5.7).

    chat (YouTube, Discord, console)
      -> pipeline (moderation gate, parser, rate limits, text filter, store)
      -> executor -> VNC -> target desktop
      -> overlay feed -> layout page in bcast

Runs as `python -m chatuses.brain` in the brain container, configured by
environment variables (images/brain/rootfs/etc/chat-uses/brain.env.tmpl):

    TARGET_HOST, VNC_PORT    the desktop chat controls
    BRAIN_BIND, BRAIN_PORT   the overlay feed and health check (0.0.0.0:8080)
    BRAIN_DB                 SQLite store (/var/lib/chat-brain/brain.db)
    BRAIN_CONSOLE            console socket (/run/chat-brain/console.sock)
    YOUTUBE_KEY_FILE         Google API key; YouTube chat is off without it
    DISCORD_TOKEN_FILE       Discord bot token; Discord is off without it
    DISCORD_GUILD_ID         ... or without the server's ID
    DISCORD_CHANNEL          the channel commands are read from (control)

Which YouTube video's chat to read is set with chat-brain-admin, and kept in
the store across restarts.
"""

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from chatuses import console
from chatuses.adapters.discord_bot import DiscordChat
from chatuses.adapters.youtube import YouTubeChat, video_id_from
from chatuses.commands import Click, Drag, InputCommand, Meta, Move, describe
from chatuses.executor import Executor, Job, JobResult, connect_to, record_in
from chatuses.identity import Platform, user_id
from chatuses.overlay import OverlayHub
from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.safety.textfilter import TextFilter
from chatuses.store import Store, to_json
from chatuses.vnc import VncInput

log = logging.getLogger("chatuses.brain")

REFUSAL_GAP = 10.0  # seconds: at most one refusal per user on stream in this time
BUSY = "the machine isn't taking input right now"
PLATFORM_NAMES = {"youtube": "YouTube", "discord": "Discord"}


@dataclass(frozen=True)
class Config:
    target_host: str
    vnc_port: int = 5900
    bind: str = "0.0.0.0"
    port: int = 8080
    db: str = "/var/lib/chat-brain/brain.db"
    console: str = "/run/chat-brain/console.sock"
    youtube_key_file: str = "/etc/chat-uses/secrets/google-api-key"
    discord_token_file: str = "/etc/chat-uses/secrets/discord-bot-token"
    discord_guild_id: int | None = None
    discord_channel: str = "control"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        target = env.get("TARGET_HOST", "")
        if not target:
            raise SystemExit("TARGET_HOST is not set")
        guild = env.get("DISCORD_GUILD_ID", "").strip()
        if guild and not guild.isdigit():
            raise SystemExit("DISCORD_GUILD_ID must be the server's numeric ID")
        return cls(
            target_host=target,
            vnc_port=int(env.get("VNC_PORT", cls.vnc_port)),
            bind=env.get("BRAIN_BIND", cls.bind),
            port=int(env.get("BRAIN_PORT", cls.port)),
            db=env.get("BRAIN_DB", cls.db),
            console=env.get("BRAIN_CONSOLE", cls.console),
            youtube_key_file=env.get("YOUTUBE_KEY_FILE", cls.youtube_key_file),
            discord_token_file=env.get("DISCORD_TOKEN_FILE", cls.discord_token_file),
            discord_guild_id=int(guild) if guild else None,
            discord_channel=env.get("DISCORD_CHANNEL", "") or cls.discord_channel,
        )


def read_secret(path: str) -> str | None:
    try:
        return Path(path).read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError as err:
        log.error("can't read %s: %s", path, err)
        return None


class Brain:
    def __init__(self, store: Store, connect: Callable[[], Awaitable[VncInput]]) -> None:
        self.store = store
        self.filter = TextFilter()
        self.pipeline = Pipeline(store, censor=self.filter)
        self.overlay = OverlayHub()
        self.executor = Executor(connect, report=self._executed, on_status=self.overlay.status)
        self._record = record_in(store)
        self._names: dict[int, str] = {}  # overlay line -> who, while its command runs
        self._last_refusal: dict[str, float] = {}
        self.youtube: YouTubeChat | None = None
        self.discord: DiscordChat | None = None

    def handle(self, msg: ChatMessage, platform: Platform) -> tuple[str, str | None]:
        """One chat message, start to finish. Returns its outcome and the reason chat sees."""
        outcome = self.pipeline.handle(msg)
        name = self.filter.safe_name(msg.user_id, msg.display_name)
        command = outcome.command
        if outcome.kind == "accepted" and isinstance(command, InputCommand):
            if not self.executor.can_take():
                # Down or swamped: a refusal like the others, so a busy chat
                # during a reboot doesn't fill the log with dead lines.
                payload = {"user": msg.user_id, "result": "busy", "command": to_json(command)}
                self.store.event(time.time(), "executed", payload)
                if self._may_show_refusal(msg):
                    self.overlay.add_line(name, platform, None, "refused", BUSY)
                return "busy", BUSY
            line = self.overlay.add_line(name, platform, describe(command), "queued")
            self._names[line] = name
            self.executor.submit(
                Job(msg.user_id, command, ref=line)
            )  # can_take() said yes, nothing ran since
        elif outcome.kind == "accepted" and command is not None:
            # Meta commands: votes, modes and time travel arrive in Phase 6.
            done = isinstance(command, Meta) and command.name in ("grid", "help", "rules")
            self.overlay.add_line(name, platform, describe(command), "done" if done else "later")
            if command == Meta("grid"):
                self.overlay.grid()
        elif outcome.kind in ("invalid", "rate_limited", "filtered") and self._may_show_refusal(msg):
            self.overlay.add_line(name, platform, None, "refused", outcome.reason)
        if outcome.timed_out_until is not None:  # always shown: chat should see what it earned
            minutes = round((outcome.timed_out_until - msg.ts) / 60)
            self.overlay.add_line(name, platform, None, "refused", f"timed out for {minutes} minutes")
        return outcome.kind, outcome.reason

    def _may_show_refusal(self, msg: ChatMessage) -> bool:
        last = self._last_refusal.get(msg.user_id)
        if last is not None and msg.ts - last < REFUSAL_GAP:
            return False
        if len(self._last_refusal) > 10_000:
            self._last_refusal = {u: t for u, t in self._last_refusal.items() if msg.ts - t < REFUSAL_GAP}
        self._last_refusal[msg.user_id] = msg.ts
        return True

    def _executed(self, job: Job, result: JobResult, seconds: float) -> None:
        self._record(job, result, seconds)
        name = self._names.pop(job.ref, "?")
        self.overlay.set_state(job.ref, "done" if result == "done" else "dropped")
        if result != "done":
            return
        match job.command:
            case Click(x=x, y=y) | Move(x=x, y=y) | Drag(end=(x, y)):
                self.overlay.pointer(x, y, name)
            case _:
                pass

    # --- chat adapters ------------------------------------------------------------

    def _deliver(self, platform: Platform) -> Callable[[ChatMessage], None]:
        def deliver(msg: ChatMessage) -> None:
            self.handle(msg, platform)

        return deliver

    def start_adapters(self, config: Config) -> None:
        """Start the chat adapters whose secrets are there; the others stay off."""
        if key := read_secret(config.youtube_key_file):
            self.youtube = YouTubeChat(key, self._deliver("yt"))
            if video := self.store.setting("youtube_video"):
                self.youtube.follow(video)
            log.info(
                "YouTube chat: %s", f"following {video}" if video else "set a video with chat-brain-admin"
            )
        else:
            log.info("YouTube chat off: no API key in %s", config.youtube_key_file)
        token = read_secret(config.discord_token_file)
        if token and config.discord_guild_id:
            self.discord = DiscordChat(
                token, config.discord_guild_id, config.discord_channel, self._deliver("dc"), self.status_text
            )
            self.discord.start()
        else:
            log.info(
                "Discord off: %s",
                "no DISCORD_GUILD_ID" if token else f"no bot token in {config.discord_token_file}",
            )

    async def stop_adapters(self) -> None:
        if self.youtube:
            await self.youtube.close()
        if self.discord:
            await self.discord.close()

    def adapters(self) -> dict[str, dict[str, Any]]:
        return {
            "youtube": self.youtube.status() if self.youtube else {"state": "off", "reason": "no API key"},
            "discord": self.discord.status()
            if self.discord
            else {"state": "off", "reason": "not configured"},
        }

    def status_text(self) -> str:
        """For people: Discord's /status and chat-brain-admin status."""
        machine = "taking commands" if self.executor.online.is_set() else "not taking input"
        lines = [f"Machine: {machine} ({self.executor.queue.qsize()} waiting)"]
        for name, status in self.adapters().items():
            errors: list[str] = status.get("recent_errors") or []
            reason = status.get("reason") or (f"last error: {errors[-1]}" if errors else "")
            if status["state"] == "streaming":
                reason = ""
            detail = f" ({reason})" if reason else ""
            video = f", video {status['video']}" if status.get("video") else ""
            quota = (
                f", {status['quota_used_today']} quota units today" if "quota_used_today" in status else ""
            )
            lines.append(f"{PLATFORM_NAMES[name]}: {status['state']}{video}{quota}{detail}")
        lines.append(f"Overlay pages: {self.overlay.clients}")
        return "\n".join(lines)

    async def admin(self, words: list[str]) -> tuple[str, str]:
        """Operator requests from chat-brain-admin (console socket: root only)."""
        match words:
            case ["status"]:
                return "ok", self.status_text()
            case ["youtube", target]:
                if self.youtube is None:
                    return "error", "no YouTube API key: see docs/phase4.md (step 4)"
                if target == "off":
                    self.youtube.follow(None)
                    self.store.set_setting("youtube_video", None)
                    return "ok", "YouTube chat: off"
                video = video_id_from(target)
                if video is None:
                    return "error", "that's not a YouTube video ID or URL"
                self.youtube.follow(video)
                self.store.set_setting("youtube_video", video)
                return "ok", f"YouTube chat: following {video} (chat-brain-admin status shows how it goes)"
            case _:
                return "error", "usage: chat-brain-admin status | youtube VIDEO_ID_OR_URL | youtube off"

    async def console_message(self, name: str, text: str) -> tuple[str, str | None]:
        try:
            uid = user_id("console", name)
        except ValueError:
            return "error", "bad name"
        return self.handle(ChatMessage(uid, name, text, time.time()), "console")

    def web_app(self) -> web.Application:
        async def health(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "ok": True,
                    "target": self.executor.online.is_set(),
                    "overlay_pages": self.overlay.clients,
                    "chat": self.adapters(),
                }
            )

        app = web.Application()
        app.router.add_get("/ws/overlay", self.overlay.handle)
        app.router.add_get("/healthz", health)
        return app

    async def run(self, config: Config, stop: asyncio.Event) -> None:
        runner = web.AppRunner(self.web_app(), access_log=None, shutdown_timeout=3.0)
        await runner.setup()
        await web.TCPSite(runner, config.bind, config.port).start()
        Path(config.console).parent.mkdir(parents=True, exist_ok=True)
        console_server = await console.serve(Path(config.console), self.console_message, self.admin)
        executor = asyncio.create_task(self.executor.run())
        self.start_adapters(config)
        log.info(
            "up: overlay feed on %s:%d, target %s:%d",
            config.bind,
            config.port,
            config.target_host,
            config.vnc_port,
        )
        try:
            await stop.wait()
        finally:
            log.info("stopping")
            await self.overlay.close_all()  # first: pages say "Controls offline", not "machine down"
            await self.stop_adapters()
            executor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await executor
            console_server.close()
            await console_server.wait_closed()
            await runner.cleanup()


async def amain(config: Config) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    with Store(config.db) as store:
        brain = Brain(store, lambda: connect_to(config.target_host, config.vnc_port))
        await brain.run(config, stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(amain(Config.from_env(os.environ)))


if __name__ == "__main__":
    main()
