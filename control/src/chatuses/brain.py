"""brain: chat messages in, desktop input and overlay out (PLAN.md 5.7).

    chat (console now; YouTube and Discord in step 4)
      -> pipeline (moderation gate, parser, rate limits, text filter, store)
      -> executor -> VNC -> target desktop
      -> overlay feed -> layout page in bcast

Runs as `python -m chatuses.brain` in the brain container, configured by
environment variables (images/brain/rootfs/etc/chat-uses/brain.env.tmpl):

    TARGET_HOST, VNC_PORT    the desktop chat controls
    BRAIN_BIND, BRAIN_PORT   the overlay feed and health check (0.0.0.0:8080)
    BRAIN_DB                 SQLite store (/var/lib/chat-brain/brain.db)
    BRAIN_CONSOLE            console chat socket (/run/chat-brain/console.sock)
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

from aiohttp import web

from chatuses import console
from chatuses.commands import Click, Drag, InputCommand, Meta, Move, describe
from chatuses.executor import Executor, Job, JobResult, connect_to, record_in
from chatuses.identity import Platform, user_id
from chatuses.names import display_name
from chatuses.overlay import OverlayHub
from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.store import Store, to_json
from chatuses.vnc import VncInput

log = logging.getLogger("chatuses.brain")

REFUSAL_GAP = 10.0  # seconds: at most one refusal per user on stream in this time
BUSY = "the machine isn't taking input right now"


@dataclass(frozen=True)
class Config:
    target_host: str
    vnc_port: int = 5900
    bind: str = "0.0.0.0"
    port: int = 8080
    db: str = "/var/lib/chat-brain/brain.db"
    console: str = "/run/chat-brain/console.sock"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        target = env.get("TARGET_HOST", "")
        if not target:
            raise SystemExit("TARGET_HOST is not set")
        return cls(
            target_host=target,
            vnc_port=int(env.get("VNC_PORT", cls.vnc_port)),
            bind=env.get("BRAIN_BIND", cls.bind),
            port=int(env.get("BRAIN_PORT", cls.port)),
            db=env.get("BRAIN_DB", cls.db),
            console=env.get("BRAIN_CONSOLE", cls.console),
        )


class Brain:
    def __init__(self, store: Store, connect: Callable[[], Awaitable[VncInput]]) -> None:
        self.store = store
        self.pipeline = Pipeline(store)
        self.overlay = OverlayHub()
        self.executor = Executor(connect, report=self._executed, on_status=self.overlay.status)
        self._record = record_in(store)
        self._names: dict[int, str] = {}  # overlay line -> who, while its command runs
        self._last_refusal: dict[str, float] = {}

    def handle(self, msg: ChatMessage, platform: Platform) -> tuple[str, str | None]:
        """One chat message, start to finish. Returns its outcome and the reason chat sees."""
        outcome = self.pipeline.handle(msg)
        name = display_name(msg.display_name)
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

    async def console_message(self, name: str, text: str) -> tuple[str, str | None]:
        try:
            uid = user_id("console", name)
        except ValueError:
            return "error", "bad name"
        return self.handle(ChatMessage(uid, name, text, time.time()), "console")

    def web_app(self) -> web.Application:
        async def health(_request: web.Request) -> web.Response:
            return web.json_response(
                {"ok": True, "target": self.executor.online.is_set(), "overlay_pages": self.overlay.clients}
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
        console_server = await console.serve(Path(config.console), self.console_message)
        executor = asyncio.create_task(self.executor.run())
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
