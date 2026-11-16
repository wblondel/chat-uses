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
    DISCORD_ALERTS_CHANNEL   where moderators get alerts (mod-alerts)
    INCUS_URL                the host's Incus API, for forensic snapshots on panic;
                             off without it or without the certificates (INCUS_*)
    TARGET_INSTANCE          the target's instance name (default: found by TARGET_HOST)
    ADMIN_TOKEN_FILE         the admin dashboard's and API's token; both are off without it
    OCR_MODE                 screen OCR: shadow (report only, the default), enforce (hold), off
    OCR_INTERVAL, OCR_LANGS  seconds between reads (5); tesseract's languages (eng+ara)
    SCREENS_DIR              where OCR hits' screenshots are kept (/var/lib/chat-brain/screens)

Which YouTube video's chat to read is set with chat-brain-admin, and kept in
the store across restarts. Moderation (panic, bans, lockdown) is
safety/moderation.py; the dashboard and its API, dashboard.py.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from chatuses import console, dashboard
from chatuses.adapters.discord_bot import DiscordChat
from chatuses.adapters.youtube import YouTubeChat, video_id_from
from chatuses.commands import Click, Drag, InputCommand, Meta, Move, describe
from chatuses.executor import Executor, Job, JobResult, connect_to, record_in
from chatuses.identity import Platform, user_id
from chatuses.incus import Incus, IncusError
from chatuses.overlay import OverlayHub
from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.safety.moderation import MODERATOR_ROLES, Answer, Moderation
from chatuses.safety.screen_ocr import Hit, ScreenWatch, read_screen, tesseract
from chatuses.safety.strikes import Strikes
from chatuses.safety.textfilter import TextFilter
from chatuses.store import RETENTION, Store, to_json
from chatuses.vnc import VncInput

log = logging.getLogger("chatuses.brain")

REFUSAL_GAP = 10.0  # seconds: at most one refusal per user on stream in this time
BUSY = "the machine isn't taking input right now"
PLATFORM_NAMES = {"youtube": "YouTube", "discord": "Discord"}
CHAT_PLATFORMS = {"yt": "YouTube", "dc": "Discord", "console": "console"}
OCR_MODES = ("shadow", "enforce", "off")
KEEP_SCREENS = 200  # OCR hits' screenshots kept on disk
SECRETS = "/etc/chat-uses/secrets"
ADMIN_USAGE = (
    "usage: chat-brain-admin status | youtube VIDEO_ID_OR_URL | youtube off\n"
    "       | panic [REASON] | resume | lockdown on|off | bans\n"
    "       | ban WHO DURATION [REASON] | timeout WHO [MINUTES] | unban WHO | forget WHO"
)
PRUNE_EVERY = 24 * 3600.0  # seconds between retention passes


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
    discord_alerts_channel: str = "mod-alerts"
    incus_url: str = ""
    incus_project: str = "targets"
    incus_cert: str = f"{SECRETS}/incus-client.crt"
    incus_key: str = f"{SECRETS}/incus-client.key"
    incus_server_cert: str = f"{SECRETS}/incus-server.crt"
    target_instance: str = ""
    admin_token_file: str = f"{SECRETS}/admin-token"
    ocr_mode: str = "shadow"
    ocr_interval: float = 5.0
    ocr_langs: str = "eng+ara"
    screens_dir: str = "/var/lib/chat-brain/screens"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        target = env.get("TARGET_HOST", "")
        if not target:
            raise SystemExit("TARGET_HOST is not set")
        guild = env.get("DISCORD_GUILD_ID", "").strip()
        if guild and not guild.isdigit():
            raise SystemExit("DISCORD_GUILD_ID must be the server's numeric ID")
        ocr_mode = env.get("OCR_MODE", "").strip() or cls.ocr_mode
        if ocr_mode not in OCR_MODES:
            raise SystemExit(f"OCR_MODE must be one of {', '.join(OCR_MODES)}")
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
            discord_alerts_channel=env.get("DISCORD_ALERTS_CHANNEL", "") or cls.discord_alerts_channel,
            incus_url=env.get("INCUS_URL", cls.incus_url),
            incus_project=env.get("INCUS_PROJECT", "") or cls.incus_project,
            incus_cert=env.get("INCUS_CERT", cls.incus_cert),
            incus_key=env.get("INCUS_KEY", cls.incus_key),
            incus_server_cert=env.get("INCUS_SERVER_CERT", cls.incus_server_cert),
            target_instance=env.get("TARGET_INSTANCE", cls.target_instance),
            admin_token_file=env.get("ADMIN_TOKEN_FILE", cls.admin_token_file),
            ocr_mode=ocr_mode,
            ocr_interval=max(1.0, float(env.get("OCR_INTERVAL", "") or cls.ocr_interval)),
            ocr_langs=env.get("OCR_LANGS", "").strip() or cls.ocr_langs,
            screens_dir=env.get("SCREENS_DIR", "") or cls.screens_dir,
        )

    def incus(self) -> tuple[Incus | None, str]:
        """brain's Incus access, or None and why not."""
        if not self.incus_url:
            return None, "INCUS_URL isn't set"
        missing = [
            f for f in (self.incus_cert, self.incus_key, self.incus_server_cert) if not Path(f).is_file()
        ]
        if missing:
            return None, f"missing {', '.join(missing)}"
        try:
            return Incus(
                self.incus_url, self.incus_cert, self.incus_key, self.incus_server_cert, self.incus_project
            ), ""
        except (OSError, ValueError) as err:  # ssl.SSLError is an OSError
            return None, f"can't load the certificates: {err}"


def read_secret(path: str) -> str | None:
    try:
        return Path(path).read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError as err:
        log.error("can't read %s: %s", path, err)
        return None


class Brain:
    def __init__(
        self,
        store: Store,
        connect: Callable[[], Awaitable[VncInput]],
        incus: Incus | None = None,
        target_host: str = "",
        target_instance: str = "",
    ) -> None:
        self.store = store
        self.filter = TextFilter()
        self.strikes = Strikes()
        self.overlay = OverlayHub()
        self.executor = Executor(connect, report=self._executed, on_status=self.overlay.status)
        self.moderation = Moderation(store, self, self.strikes, self.filter.safe_name)
        self.pipeline = Pipeline(
            store,
            censor=self.filter,
            strikes=self.strikes,
            locked_out=self.moderation.held_by_lockdown,
            paused=lambda: self.moderation.panicking,
        )
        self.incus = incus
        self.incus_off_reason = "" if incus else "not set up"
        self.target_host = target_host
        self.target_instance = target_instance
        self._record = record_in(store)
        self._names: dict[int, str] = {}  # overlay line -> who, while its command runs
        self._last_refusal: dict[str, float] = {}
        self._background: set[asyncio.Task[None]] = set()
        self.youtube: YouTubeChat | None = None
        self.discord: DiscordChat | None = None
        self.watch: ScreenWatch | None = None  # screen OCR (start_ocr)
        self.ocr_mode = "off"
        self.screens_dir: Path | None = None
        if self.moderation.panicking:  # a panic outlives a restart
            self.panic_changed(True, self.moderation.kind)
        if self.moderation.locked:
            self.overlay.lockdown(True)

    def handle(self, msg: ChatMessage, platform: Platform) -> tuple[str, str | None]:
        """One chat message, start to finish. Returns its outcome and the reason chat sees."""
        if msg.roles & MODERATOR_ROLES and (answer := self._chat_moderation(msg, platform)):
            return ("moderation" if answer.ok else "invalid"), answer.text
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

    def _chat_moderation(self, msg: ChatMessage, platform: Platform) -> Answer | None:
        """`!panic [reason]` and `!resume` from moderators in chat: YouTube's have no slash commands."""
        word, _, rest = msg.text.strip().partition(" ")
        if word.lower() not in ("!panic", "!resume"):
            return None
        name = self.filter.safe_name(msg.user_id, msg.display_name)
        by = f"{name} ({msg.user_id}) in {CHAT_PLATFORMS[platform]} chat"
        if word.lower() == "!panic":
            return self.moderation.panic(by, rest)
        return self.moderation.resume(by)

    # --- what moderation does to the stream (safety/moderation.Effects) --------------

    def panic_changed(self, on: bool, kind: str = "panic") -> None:
        self.overlay.panic(on, kind)  # the page covers the screen, bcast mutes the target
        if on:
            self.executor.freeze()
            return
        self.executor.thaw()
        ended = self.moderation.last_panic or {}
        if kind == "hold" and self.watch:  # a moderator said what OCR saw is fine
            self.watch.clear(ended.get("lines") or [])

    def lockdown_changed(self, on: bool) -> None:
        self.overlay.lockdown(on)

    def alert(self, text: str, image: bytes | None = None) -> None:
        log.warning("moderation: %s", text)
        if self.discord:
            self.discord.alert(text, image)

    def snapshot(self, name: str) -> None:
        if self.incus is None:
            self.store.event(time.time(), "snapshot", {"name": name, "result": "off"})
            self.alert(f"No forensic snapshot: brain has no Incus access ({self.incus_off_reason}).")
            return
        task = asyncio.create_task(self._snapshot(self.incus, name))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _snapshot(self, incus: Incus, name: str) -> None:
        start = time.monotonic()
        try:
            instance = await incus.snapshot(name, instance=self.target_instance, address=self.target_host)
        except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
            reason = str(err) or type(err).__name__
            self.store.event(time.time(), "snapshot", {"name": name, "result": "failed", "error": reason})
            self.alert(f"Forensic snapshot `{name}` failed: {reason}")
            return
        ms = round((time.monotonic() - start) * 1000)
        self.store.event(
            time.time(), "snapshot", {"name": name, "instance": instance, "result": "ok", "ms": ms}
        )
        self.alert(f"📸 Forensic snapshot `{instance}/{name}` taken (kept private: chat can't rewind to it).")

    # --- screen OCR (safety/screen_ocr.py) --------------------------------------------

    def start_ocr(self, config: Config) -> None:
        if config.ocr_mode == "off":
            log.info("screen OCR off (OCR_MODE=off)")
            return
        if not shutil.which("tesseract"):
            log.error("screen OCR off: tesseract isn't installed")
            return
        self.ocr_mode = config.ocr_mode
        self.screens_dir = Path(config.screens_dir)
        langs = config.ocr_langs
        self.watch = ScreenWatch(
            connect=lambda: read_screen(config.target_host, config.vnc_port),
            ocr=lambda frame: tesseract(frame, langs),
            matches=lambda text: bool(self.filter.lists.count(text)),
            on_hit=self.ocr_hit,
            paused=lambda: self.moderation.panicking,
            interval=config.ocr_interval,
        )
        log.info("screen OCR: %s mode, every %.0f s (%s)", self.ocr_mode, config.ocr_interval, langs)

    def ocr_hit(self, hit: Hit) -> None:
        """Screen OCR saw a listed word: report it (shadow), or hold the stream (enforce)."""
        shot = self._keep_screenshot(hit)
        enforce = self.ocr_mode == "enforce"
        self.store.event(
            hit.ts, "ocr_hit", {"mode": self.ocr_mode, "found": list(hit.found), "screenshot": shot}
        )
        if enforce:
            self.moderation.hold("screen OCR", hit.lines, now=hit.ts)
        seen = "\n".join(f"||{line[:300]}||" for line in hit.found[:5])
        what = "held the stream" if enforce else "shadow mode, nothing was done on stream"
        self.alert(f"👁️ Screen OCR saw a listed word ({what}):\n{seen}", hit.frame.png())

    async def keep_tidy(self) -> None:
        """Retention (PLAN.md 11): raw logs and screenshots go after 90 days. Never returns."""
        while True:
            now = time.time()
            try:
                commands, events = self.store.prune(now)
                shots = 0
                if self.screens_dir and self.screens_dir.is_dir():
                    for shot in self.screens_dir.glob("ocr-*.png"):
                        if shot.stat().st_mtime < now - RETENTION:
                            shot.unlink()
                            shots += 1
                if commands or events or shots:
                    log.info(
                        "retention: deleted %d commands, %d events, %d screenshots", commands, events, shots
                    )
            except (OSError, sqlite3.Error) as err:
                log.error("retention: %s", err)
            await asyncio.sleep(PRUNE_EVERY)

    def _keep_screenshot(self, hit: Hit) -> str | None:
        if self.screens_dir is None:
            return None
        name = "ocr-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(hit.ts)) + ".png"
        try:
            self.screens_dir.mkdir(parents=True, exist_ok=True)
            (self.screens_dir / name).write_bytes(hit.frame.png())
            for old in sorted(self.screens_dir.glob("ocr-*.png"))[:-KEEP_SCREENS]:
                old.unlink()
        except OSError as err:
            log.error("screen OCR: can't keep the screenshot: %s", err)
            return None
        return name

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
                token,
                config.discord_guild_id,
                config.discord_channel,
                self._deliver("dc"),
                self.status_text,
                self.moderation,
                config.discord_alerts_channel,
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
        if self.executor.frozen:
            machine = "input frozen by the panic"
        lines = [f"Machine: {machine} ({self.executor.queue.qsize()} waiting)"]
        if panic := self.moderation.panic_state:
            what = "CONTENT HOLD" if self.moderation.kind == "hold" else "PANIC"
            lines.append(
                f"{what} since {_clock(panic.get('since', 0))}, by {panic.get('by')}: /resume ends it"
            )
        if lockdown := self.moderation.lockdown_state:
            lines.append(f"Lockdown on since {_clock(lockdown.get('since', 0))}, by {lockdown.get('by')}")
        if self.watch is None:
            lines.append("Screen OCR: off")
        else:
            ocr = self.watch.status()
            last = f", last read {_clock(ocr['last_read'])} in {ocr['last_ms']} ms" if ocr["reads"] else ""
            error = f", last error: {ocr['last_error']}" if ocr["last_error"] else ""
            lines.append(f"Screen OCR: {self.ocr_mode}, {ocr['state']}, {ocr['hits']} hits{last}{error}")
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
            case ["panic", *reason]:
                return _answer(self.moderation.panic("console", " ".join(reason)))
            case ["resume"]:
                return _answer(self.moderation.resume("console"))
            case ["lockdown", ("on" | "off") as state]:
                return _answer(self.moderation.lockdown("console", state == "on"))
            case ["ban", who, duration, *reason]:
                return _answer(self.moderation.ban("console", who, duration, " ".join(reason)))
            case ["timeout", who]:
                return _answer(self.moderation.timeout("console", who))
            case ["timeout", who, minutes] if minutes.isdigit():
                return _answer(self.moderation.timeout("console", who, int(minutes)))
            case ["unban", who]:
                return _answer(self.moderation.unban("console", who))
            case ["forget", who]:
                found = self.moderation.resolve(who, time.time())
                if isinstance(found, Answer):
                    return _answer(found)
                commands, events = self.store.forget(time.time(), found)
                self.strikes.forget(found)
                return "ok", f"Forgot {found}: {commands} commands and {events} log entries deleted."
            case ["bans"]:
                rows = self.moderation.state()["banned"]
                if not rows:
                    return "ok", "Nobody is banned or timed out."
                return "ok", "\n".join(
                    f"{r['name'] or '?'} ({r['user']}) "
                    + (f"until {_clock(r['until'])}" if r["until"] else "forever")
                    for r in rows
                )
            case _:
                return "error", ADMIN_USAGE

    async def console_message(self, name: str, text: str) -> tuple[str, str | None]:
        try:
            uid = user_id("console", name)
        except ValueError:
            return "error", "bad name"
        return self.handle(ChatMessage(uid, name, text, time.time()), "console")

    def web_app(self, admin_token: str | None = None) -> web.Application:
        async def health(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "ok": True,
                    "target": self.executor.online.is_set(),
                    "overlay_pages": self.overlay.clients,
                    "chat": self.adapters(),
                    "ocr": {"mode": self.ocr_mode, **self.watch.status()} if self.watch else {"mode": "off"},
                }
            )

        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/ws/overlay", self.overlay.handle)
        app.router.add_get("/healthz", health)
        if admin_token:
            dashboard.add_routes(app, self, admin_token)
        return app

    async def run(self, config: Config, stop: asyncio.Event) -> None:
        token = read_secret(config.admin_token_file)
        if not token:
            log.info("admin dashboard off: no token in %s", config.admin_token_file)
        elif len(token) < 32:
            log.error(
                "admin dashboard off: the token in %s is too short (32+ characters)", config.admin_token_file
            )
            token = None
        runner = web.AppRunner(self.web_app(token), access_log=None, shutdown_timeout=3.0)
        await runner.setup()
        await web.TCPSite(runner, config.bind, config.port).start()
        Path(config.console).parent.mkdir(parents=True, exist_ok=True)
        console_server = await console.serve(Path(config.console), self.console_message, self.admin)
        executor = asyncio.create_task(self.executor.run())
        self.start_adapters(config)
        self.start_ocr(config)
        tidy = asyncio.create_task(self.keep_tidy())
        self._background.add(tidy)
        tidy.add_done_callback(self._background.discard)
        if self.watch:
            watch = asyncio.create_task(self.watch.run())
            self._background.add(watch)
            watch.add_done_callback(self._background.discard)
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
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)
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
    incus, why_not = config.incus()
    if incus is None:
        log.info("forensic snapshots off: %s", why_not)
    with Store(config.db) as store:
        brain = Brain(
            store,
            lambda: connect_to(config.target_host, config.vnc_port),
            incus,
            config.target_host,
            config.target_instance,
        )
        brain.incus_off_reason = why_not
        await brain.run(config, stop)


def _answer(answer: Answer) -> tuple[str, str]:
    return ("ok" if answer.ok else "error"), answer.text


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(amain(Config.from_env(os.environ)))


if __name__ == "__main__":
    main()
