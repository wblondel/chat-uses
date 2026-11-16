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
    DISCORD_OPS_CHANNEL      where the watchdog reports the target's health (ops)
    INCUS_URL                the host's Incus API, for forensic snapshots on panic;
                             off without it or without the certificates (INCUS_*)
    TARGET_INSTANCE          the target's instance name (default: found by TARGET_HOST)
    ADMIN_TOKEN_FILE         the admin dashboard's and API's token; both are off without it
    OCR_MODE                 screen OCR: shadow (report only, the default), enforce (hold), off
    OCR_INTERVAL, OCR_LANGS  seconds between reads (5); tesseract's languages (eng+ara)
    SCREENS_DIR              where OCR hits' screenshots are kept (/var/lib/chat-brain/screens)
    RULESETS_DIR             the rulesets (/usr/lib/chat-brain/rulesets); which one is in the store
    THUMBS_DIR               snapshots' thumbnails for the overlay (/var/lib/chat-brain/thumbs)

Which YouTube video's chat to read is set with chat-brain-admin, and kept in
the store across restarts. Moderation (panic, bans, lockdown) is
safety/moderation.py; the dashboard and its API, dashboard.py. Power votes,
rulesets and laws are the referee's (referee/).
"""

import asyncio
import contextlib
import logging
import os
import re
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
from chatuses.commands import Click, Drag, InputCommand, Load, Meta, Move, Rewind, Save, describe
from chatuses.executor import Executor, Job, JobResult, connect_to, record_in
from chatuses.identity import Platform, user_id
from chatuses.incus import Incus, IncusError
from chatuses.overlay import OverlayHub
from chatuses.pipeline import PAUSED, ChatMessage, Pipeline
from chatuses.referee import laws
from chatuses.referee.laws import DEFAULT, Ruleset, RulesetError
from chatuses.referee.modes import Winner
from chatuses.referee.referee import Referee
from chatuses.referee.votes import Proposal
from chatuses.safety.moderation import MODERATOR_ROLES, Answer, Moderation
from chatuses.safety.screen_ocr import Hit, ScreenWatch, read_screen, tesseract
from chatuses.safety.strikes import Strikes
from chatuses.safety.textfilter import TextFilter
from chatuses.safety.watchdog import Watchdog
from chatuses.screen import Frame
from chatuses.snapshots import Snap, Snapshots
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
    "       | ban WHO DURATION [REASON] | timeout WHO [MINUTES] | unban WHO | forget WHO\n"
    "       | endvote | mode anarchy|democracy | ruleset [NAME] | laws | law NAME VALUE|default\n"
    "       | snapshots | snapshot | rewind-to NUMBER|SAVE"
)
OVERLAY_ONLY = ("grid", "help", "rules")  # meta commands the overlay answers by itself
# Counted in the vote panel, not each put in the log: a vote would push every command off it.
BALLOTS = ("yes", "no", "veto")
REFEREE_TICK = 0.25  # seconds between the vote engine's checks
AUTO_CHECK = 30.0  # seconds between looks at whether an auto snapshot is due
TRAVELLING = "wait: the machine is travelling in time"
NO_INCUS = "time travel is off: brain has no Incus access"
NO_REBOOT = "the machine didn't shut down within 30 s: !reset forces it"
_THUMB = re.compile(r"\d{1,9}\.png")
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
    discord_ops_channel: str = "ops"
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
    rulesets_dir: str = "/usr/lib/chat-brain/rulesets"
    thumbs_dir: str = "/var/lib/chat-brain/thumbs"

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
            discord_ops_channel=env.get("DISCORD_OPS_CHANNEL", "") or cls.discord_ops_channel,
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
            rulesets_dir=env.get("RULESETS_DIR", "") or cls.rulesets_dir,
            thumbs_dir=env.get("THUMBS_DIR", "") or cls.thumbs_dir,
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
        ruleset: Ruleset = DEFAULT,
        rulesets: Path | None = None,
        thumbs: Path | None = None,
        grab: Callable[[], Awaitable[Frame | None]] | None = None,
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
            # Banned users aren't active: they don't raise a vote's quorum.
            is_banned=lambda user: store.banned_until(user) > time.time(),
            locked_out=self.moderation.held_by_lockdown,
            paused=lambda: self.moderation.panicking,
        )
        self.referee = Referee(
            store,
            self.pipeline.presence,
            self,
            self.overlay.vote,
            rulesets,
            ruleset,
            show_mode=self.overlay.mode,
            run_winner=self._democracy_won,
        )
        self.overlay.mode(self.referee.modes.view(time.time()))  # pages that connect get it
        self.incus = incus
        self.incus_off_reason = "" if incus else "not set up"
        self.target_host = target_host
        self.target_instance = target_instance
        self.thumbs = thumbs
        self.snapshots: Snapshots | None = None  # chat's time travel (snapshots.py): needs Incus
        if incus is not None:
            self.snapshots = Snapshots(
                store, incus, self._instance, lambda: self.referee.laws, thumbs, grab, self._timeline_changed
            )
            self._timeline_changed()
        self._found_instance = ""
        self._travelling = False  # a rewind, load, reboot or reset under way
        self._started = time.time()
        self._record = record_in(store)
        self._names: dict[int, str] = {}  # overlay line -> who, while its command runs
        self._last_refusal: dict[str, float] = {}
        self._background: set[asyncio.Task[None]] = set()
        self.youtube: YouTubeChat | None = None
        self.discord: DiscordChat | None = None
        self.watch: ScreenWatch | None = None  # screen OCR (start_ocr)
        self.ops_channel = "ops"
        self.watchdog = Watchdog(
            incus,
            target_host,
            target_instance,
            vnc_up=self.executor.online.is_set,
            last_input=lambda: self.executor.last_input,
            last_change=lambda: self.watch.last_change if self.watch else None,
            alert=self.ops,
        )
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
        if outcome.kind == "accepted" and isinstance(command, InputCommand) and self.referee.democracy:
            # A ballot: the overlay's tally shows it; the window's winner runs (_democracy_won).
            self.referee.ballot(msg.user_id, name, command, msg.ts)
        elif outcome.kind == "accepted" and isinstance(command, InputCommand):
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
        elif outcome.kind == "accepted" and isinstance(command, Meta) and command.name in OVERLAY_ONLY:
            self.overlay.add_line(name, platform, describe(command), "done")
            if command == Meta("grid"):
                self.overlay.grid()
        elif outcome.kind == "accepted" and isinstance(command, (Meta, Rewind, Load, Save)):  # referee/
            reply = self.referee.handle(msg.user_id, name, command, msg.ts)
            if not reply.ok:
                if self._may_show_refusal(msg):
                    self.overlay.add_line(name, platform, None, "refused", reply.text)
                return "refused", reply.text
            if not (isinstance(command, Meta) and command.name in BALLOTS):
                self.overlay.add_line(name, platform, describe(command), "done")
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
            self.referee.end_vote("cancelled: the stream is paused")
            self.referee.modes.clear()  # ballots for the screen before
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

    def ops(self, text: str) -> None:
        """The target's health, for #ops (safety/watchdog.py)."""
        log.warning("watchdog: %s", text)
        if self.discord:
            self.discord.alert(text, channel=self.ops_channel)

    def end_vote(self, why: str) -> tuple[bool, str]:
        reply = self.referee.end_vote(why)
        return reply.ok, reply.text

    def set_mode(self, mode: str, by: str) -> tuple[bool, str]:
        if mode not in ("anarchy", "democracy"):
            return False, "anarchy or democracy"
        if "democracy" not in self.referee.ruleset.features:
            return False, "Democracy is off in this ruleset."
        reply = self.referee.set_mode("democracy" if mode == "democracy" else "anarchy", by)
        return reply.ok, reply.text

    def _democracy_won(self, winner: Winner) -> None:
        """The window's most-voted command runs, as chat's (PLAN.md 6.3)."""
        label = f"democracy ({winner.votes}/{winner.total})"
        if not self.executor.can_take():
            self.overlay.add_line(label, "democracy", winner.text, "refused", BUSY)
            return
        line = self.overlay.add_line(label, "democracy", winner.text, "queued")
        self._names[line] = "democracy"
        self.executor.submit(Job(winner.ballot.user, winner.ballot.command, ref=line))

    # --- what a passed vote does (referee.Actions) -------------------------------------

    def unavailable(self, proposal: Proposal) -> str | None:
        action, arg = proposal.action, proposal.arg
        if self.moderation.panicking:
            return PAUSED
        if action == "newrun":
            return "runs come later: !newrun isn't on yet"
        if action == "impeach":
            return "nobody is driving: the driver's seat comes later"
        if self.snapshots is None:
            return NO_INCUS
        if self._travelling:
            return TRAVELLING
        if action == "rewind" and self.snapshots.auto(int(arg or 1)) is None:
            have = len(self.snapshots.all("auto"))
            return f"only {have} snapshots to go back to" if have else "no snapshot to go back to yet"
        if action == "load" and self.snapshots.named(str(arg)) is None:
            return f"no save called {arg}"
        return None

    async def run(self, proposal: Proposal) -> tuple[bool, str]:
        """What a passed vote does: a few words for chat."""
        if self.snapshots is None:
            return False, NO_INCUS
        snapshots = self.snapshots
        try:
            match proposal.action:
                case "save":
                    snap = await snapshots.take("named", str(proposal.arg))
                    return True, f"saved as {snap.label}"
                case "rewind" | "load":
                    snap = (
                        snapshots.auto(int(proposal.arg or 1))
                        if proposal.action == "rewind"
                        else (snapshots.named(str(proposal.arg)))
                    )
                    if snap is None:
                        return False, "that snapshot is gone"
                    await self._travel(snap)
                    return True, f"back to {snap.label}"
                case "reboot" | "reset":
                    return await self._power(proposal.action == "reset")
                case _:
                    return False, f"{proposal.text} can't be done"
        except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
            reason = str(err) or type(err).__name__
            self.ops(f"⚠️ {proposal.text} (voted by chat) failed: {reason}")
            return False, "it didn't work (the moderators know)"

    async def _instance(self) -> str:
        """The target's instance: TARGET_INSTANCE, or found by its address once."""
        if self.target_instance:
            return self.target_instance
        if not self._found_instance:
            assert self.incus is not None
            self._found_instance = await self.incus.find(self.target_host)
        return self._found_instance

    async def _travel(self, snap: Snap) -> None:
        """Put the target back to a snapshot. Input queued for the screen before is dropped."""
        assert self.snapshots is not None
        self._travelling = True
        self.executor.freeze()
        self.referee.modes.clear()
        try:
            await self.snapshots.restore(snap)
        finally:
            self._travelling = False
            if not self.moderation.panicking:
                self.executor.thaw()

    async def _power(self, force: bool) -> tuple[bool, str]:
        assert self.incus is not None
        self._travelling = True
        self.executor.freeze()
        self.referee.modes.clear()
        try:
            await self.incus.restart(await self._instance(), force=force)
        except IncusError as err:
            if not force and "shutting down" in str(err).lower():
                return False, NO_REBOOT
            raise
        finally:
            self._travelling = False
            if not self.moderation.panicking:
                self.executor.thaw()
        self.store.event(time.time(), "power", {"action": "reset" if force else "reboot"})
        return True, "reset" if force else "rebooted"

    def rewind_to(self, which: str, by: str) -> tuple[bool, str]:
        """A moderator's /rewind-to: any snapshot of chat's, no vote; in the background."""
        if self.snapshots is None:
            return False, NO_INCUS.capitalize() + "."
        snap = self.snapshots.find(which)
        if snap is None:
            return False, f"No snapshot {which!r}: the dashboard (or chat-brain-admin snapshots) lists them."
        if self._travelling:
            return False, TRAVELLING.capitalize() + "."
        self.referee.end_vote(f"ended: {by} rewound")
        task = asyncio.get_running_loop().create_task(self._rewind_to(snap, by))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return True, f"Rewinding to #{snap.id} ({snap.label})…"

    async def _rewind_to(self, snap: Snap, by: str) -> None:
        try:
            await self._travel(snap)
        except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
            self.alert(f"⏪ Rewind to #{snap.id} ({snap.label}) by {by} failed: {err or type(err).__name__}")
            return
        self.alert(f"⏪ {by} rewound the machine to #{snap.id} ({snap.label}).")

    def _timeline_changed(self) -> None:
        if self.snapshots is not None:
            self.overlay.timeline(self.snapshots.view())

    def _auto_due(self, now: float) -> bool:
        """An auto snapshot now? Due, the target healthy, and its screen changed since the last (6.6)."""
        if self.snapshots is None or self.moderation.panicking or self._travelling:
            return False
        vote = self.referee.votes.current
        if vote is not None and vote.phase == "running":
            return False
        last = self.snapshots.latest_auto_time() or self._started
        if now - last < self.referee.laws.auto_snapshot_min * 60:
            return False
        if not self.executor.online.is_set() or self.watchdog.problems():
            return False
        changed = self.watch.last_change if self.watch else (self.executor.last_input or None)
        return changed is not None and changed > last

    async def _auto_snapshots(self) -> None:
        """Never returns."""
        failing = False
        while True:
            await asyncio.sleep(AUTO_CHECK)
            if self.snapshots is None or not self._auto_due(time.time()):
                continue
            try:
                await self.snapshots.take("auto")
                failing = False
            except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
                log.warning("auto snapshot: %s", err)
                if not failing:  # once, until one works again
                    self.ops(f"⚠️ Auto snapshot failed: {err or type(err).__name__}")
                failing = True

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
        lines.append(self.referee.status_line())
        if self.snapshots is None:
            lines.append(f"Snapshots: off ({self.incus_off_reason})")
        else:
            autos, saves = self.snapshots.all("auto"), self.snapshots.all("named")
            latest = f", latest {autos[0].label}" if autos else ""
            names = f" ({', '.join(s.label for s in saves)})" if saves else ""
            lines.append(f"Snapshots: {len(autos)} auto{latest}; {len(saves)} saves{names}")
        health = self.watchdog.status()
        lines.append(f"Watchdog: {', '.join(health['problems']) or 'healthy'}")
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
            case ["snapshots"]:
                if self.snapshots is None:
                    return "error", NO_INCUS
                rows = self.snapshots.all()
                if not rows:
                    return "ok", "No snapshots yet."
                return "ok", "\n".join(
                    f"#{s.id} {s.kind} {s.label} ({_clock(s.created)}, {s.ref})" for s in rows
                )
            case ["snapshot"]:
                if self.snapshots is None:
                    return "error", NO_INCUS
                try:
                    snap = await self.snapshots.take("auto")
                except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
                    return "error", f"Snapshot failed: {err or type(err).__name__}"
                return "ok", f"Snapshot #{snap.id} taken ({snap.ref})."
            case ["rewind-to", which]:
                return _answer(self.moderation.rewind_to("console", which))
            case ["endvote"]:
                return _answer(self.moderation.endvote("console"))
            case ["mode", ("anarchy" | "democracy") as mode]:
                return _answer(self.moderation.mode("console", mode))
            case ["ruleset"]:
                names = ", ".join(laws.available(self.referee.rulesets)) if self.referee.rulesets else "?"
                return "ok", f"Ruleset: {self.referee.ruleset.name} (available: {names})"
            case ["ruleset", name]:
                try:
                    return "ok", self.referee.set_ruleset(name, "console")
                except RulesetError as err:
                    return "error", f"Ruleset unchanged: {err}"
            case ["laws"]:
                changed = self.store.laws()
                if not changed:
                    return "ok", f"All laws are the ruleset's ({self.referee.ruleset.name})."
                return "ok", "\n".join(f"{key} = {value:g}" for key, value in sorted(changed.items()))
            case ["law", key, value]:
                try:
                    return "ok", self.referee.set_law(key, value, "console")
                except RulesetError as err:
                    return "error", f"Law unchanged: {err}"
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
                    "watchdog": self.watchdog.status(),
                }
            )

        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/ws/overlay", self.overlay.handle)
        app.router.add_get("/healthz", health)
        app.router.add_get("/thumbs/{name}", self._thumb)
        if admin_token:
            dashboard.add_routes(app, self, admin_token)
        return app

    async def _thumb(self, request: web.Request) -> web.StreamResponse:
        """A snapshot's thumbnail, for the overlay's timeline: a past frame of the stream (D37)."""
        name = request.match_info["name"]
        if self.thumbs is None or not _THUMB.fullmatch(name):
            raise web.HTTPNotFound()
        path = self.thumbs / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400", "Content-Type": "image/png"})

    async def _referee_ticks(self) -> None:
        """Close vote windows and veto countdowns on time. Never returns."""
        while True:
            self.referee.tick()
            await asyncio.sleep(REFEREE_TICK)

    async def serve(self, config: Config, stop: asyncio.Event) -> None:
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
        self.ops_channel = config.discord_ops_channel
        if self.snapshots is not None:
            try:
                await self.snapshots.reconcile()
            except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
                log.warning("snapshots: can't list the target's: %s", err)
        for job in (self.keep_tidy(), self.watchdog.run(), self._referee_ticks(), self._auto_snapshots()):
            task = asyncio.create_task(job)
            self._background.add(task)
            task.add_done_callback(self._background.discard)
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
        rulesets = Path(config.rulesets_dir)
        name = store.setting("ruleset") or DEFAULT.name
        try:
            ruleset = laws.load(rulesets, name)
        except RulesetError as err:
            log.error("ruleset %s: %s (using the built-in defaults)", name, err)
            ruleset = DEFAULT

        async def grab() -> Frame | None:
            reader = await read_screen(config.target_host, config.vnc_port)
            try:
                return await reader.grab()
            finally:
                await reader.close()

        brain = Brain(
            store,
            lambda: connect_to(config.target_host, config.vnc_port),
            incus,
            config.target_host,
            config.target_instance,
            ruleset,
            rulesets,
            Path(config.thumbs_dir),
            grab,
        )
        brain.incus_off_reason = why_not
        await brain.serve(config, stop)


def _answer(answer: Answer) -> tuple[str, str]:
    return ("ok" if answer.ok else "error"), answer.text


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(amain(Config.from_env(os.environ)))


if __name__ == "__main__":
    main()
