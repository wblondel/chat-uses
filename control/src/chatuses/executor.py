"""Runs accepted input commands on the target, one at a time (PLAN.md 5.7).

Anarchy mode: arrival order. The queue is bounded: when it's full, new
commands are refused rather than piling up behind a slow target. If the
target goes away (reboot, rewind), whatever was waiting is dropped, not
replayed onto a fresh desktop later, and the executor reconnects in the
background.

A panic freezes it (PLAN.md 7.5): the command running stops where it is,
with every key and button let go, the waiting ones are dropped, and nothing
new is taken until it thaws.

Grid coordinates are scaled to the screen size the VNC server reports, so a
target at another resolution still gets clicks where chat aimed.

A macro is one job: its steps run one after the other, MACRO_GAP apart, and
nothing else runs in between; its time limit is one command's per step.

`!click "text"` is found on the screen when its turn comes, not when chat
sent it (textclick.py, through `resolve`): the screen it was aimed at is the
one it lands on. Not there: the job ends "missed", nothing sent.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from chatuses.commands import (
    Click,
    ClickText,
    Drag,
    InputCommand,
    MacroRun,
    Move,
    PressKeys,
    Scroll,
    TypeText,
)
from chatuses.grid import Grid
from chatuses.keymap import char_keysym
from chatuses.store import Store, to_json
from chatuses.vnc import BUTTONS, VncError, VncInput

JobResult = Literal["done", "offline", "flushed", "missed"]  # flushed: a panic; missed: text not seen
KEY_DELAY = 0.008  # seconds between keystrokes
JOB_TIMEOUT = 10.0  # seconds for one command; the longest takes about one
MACRO_GAP = 0.2  # seconds between a macro's steps: what one opens can take a keypress
RECONNECT = (1.0, 15.0)  # first and longest wait between connection attempts
RESOLVE_TIMEOUT = 15.0  # seconds to find !click "text" on the screen (a frame, two OCR readings)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    user_id: str
    command: InputCommand
    accepted_at: float = field(default_factory=time.monotonic)
    ref: int = 0  # the caller's, e.g. the overlay line showing this command


# Called once per job with its result and the seconds from acceptance to the
# last input event sent (or to giving up).
Report = Callable[[Job, JobResult, float], None]
# Where !click "text" lands, in grid coordinates (None: not on the screen).
Resolve = Callable[[ClickText], Awaitable[Click | None]]


async def nowhere(_command: ClickText) -> Click | None:
    return None


def ignore(_job: Job, _result: JobResult, _seconds: float) -> None:
    pass


def no_status(_online: bool) -> None:
    pass


def record_in(store: Store) -> Report:
    """A report that appends an `executed` event per job."""

    def report(job: Job, result: JobResult, seconds: float) -> None:
        store.event(
            time.time(),
            "executed",
            {
                "user": job.user_id,
                "result": result,
                "ms": round(seconds * 1000),
                "command": to_json(job.command),
            },
        )

    return report


class Executor:
    def __init__(
        self,
        connect: Callable[[], Awaitable[VncInput]],
        grid: Grid | None = None,
        max_pending: int = 16,
        report: Report = ignore,
        on_status: Callable[[bool], None] = no_status,
        delay: Callable[[], float] = lambda: 0.0,
        resolve: Resolve = nowhere,
    ) -> None:
        self._connect = connect
        self.grid = grid or Grid()
        self.report = report
        self.on_status = on_status  # called with True when the target's input is up, False when it goes
        self.delay = delay  # seconds to wait before each command (chaos's input lag)
        self.resolve = resolve  # brain's: !click "text" on the screen as it is now
        self.queue: asyncio.Queue[Job] = asyncio.Queue(max_pending)
        self.online = asyncio.Event()
        self.vnc: VncInput | None = None
        self.frozen = False
        self.last_input = 0.0  # when a command last reached the target (for the watchdog)
        self._running: asyncio.Task[tuple[JobResult, InputCommand]] | None = None

    def can_take(self) -> bool:
        """Would submit() accept a job right now? (Nothing changes before the next await.)"""
        return self.online.is_set() and not self.frozen and not self.queue.full()

    def submit(self, job: Job) -> bool:
        """Queue a command; False if the target is offline, frozen, or the queue is full."""
        if not self.online.is_set() or self.frozen:
            return False
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            return False
        return True

    def freeze(self) -> None:
        """Panic: stop the command running, drop the waiting ones, take nothing new."""
        self.frozen = True
        if self._running and not self._running.done():
            self._running.cancel()  # _serve lets go of the keys and reports it
        while not self.queue.empty():
            self._finish(self.queue.get_nowait(), "flushed")

    def thaw(self) -> None:
        self.frozen = False

    async def run(self) -> None:
        """Connect, run jobs until the connection drops, start over. Never returns."""
        while True:
            vnc = await self._reconnect()
            self.vnc = vnc
            self.online.set()
            self._status(True)
            try:
                await self._serve(vnc)
            finally:
                self._offline()
                await vnc.close()

    async def _reconnect(self) -> VncInput:
        wait = RECONNECT[0]
        while True:
            try:
                return await self._connect()
            except VncError:
                await asyncio.sleep(wait)
                wait = min(wait * 2, RECONNECT[1])

    async def _serve(self, vnc: VncInput) -> None:
        lost = asyncio.create_task(vnc.lost.wait())
        get: asyncio.Task[Job] | None = None
        try:
            while True:
                get = asyncio.create_task(self.queue.get())
                await asyncio.wait({get, lost}, return_when=asyncio.FIRST_COMPLETED)
                if not get.done():
                    return  # connection lost while idle
                job = get.result()
                if self.frozen:  # taken just as a panic began
                    self._finish(job, "flushed")
                    continue
                wait = self.delay()  # chaos's input lag (game/chaos.py)
                self._running = asyncio.create_task(_after(wait, self._run(vnc, job.command)))
                try:
                    result, ran = await asyncio.wait_for(
                        asyncio.shield(self._running), _timeout(job.command) + wait
                    )
                except asyncio.CancelledError:
                    me = asyncio.current_task()
                    if not self._running.cancelled() or (me is not None and me.cancelling()):
                        raise  # brain is stopping, not a panic
                    try:  # frozen half-way: nothing may stay pressed
                        await asyncio.wait_for(vnc.release(), JOB_TIMEOUT)
                    except (VncError, TimeoutError):
                        self._finish(job, "flushed")
                        return
                    self._finish(job, "flushed")
                    continue
                except (VncError, TimeoutError):
                    self._finish(job, "offline")
                    return
                self._finish(replace(job, command=ran), result)  # !click "text": the click it became
        finally:
            lost.cancel()
            if self._running and not self._running.done():
                self._running.cancel()
                with contextlib.suppress(asyncio.CancelledError, VncError):
                    await self._running
            self._running = None
            if get and not get.done():  # cancelling a get leaves its job in the queue
                get.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await get

    async def _run(self, vnc: VncInput, command: InputCommand) -> tuple[JobResult, InputCommand]:
        """The job's result, and what was really sent."""
        if isinstance(command, ClickText):
            try:
                click = await asyncio.wait_for(self.resolve(command), RESOLVE_TIMEOUT)
            except TimeoutError:
                click = None
            if click is None:
                return "missed", command
            command = click
        await execute(vnc, command, self.grid)
        return "done", command

    def _offline(self) -> None:
        """Drop what was waiting: replaying it on a restarted target would be wrong."""
        if self.online.is_set():
            self.online.clear()
            self._status(False)
        while not self.queue.empty():
            self._finish(self.queue.get_nowait(), "offline")

    def _status(self, online: bool) -> None:
        try:
            self.on_status(online)
        except Exception:
            log.exception("reporting the executor's status failed")

    def _finish(self, job: Job, result: JobResult) -> None:
        if result == "done":
            self.last_input = time.time()
        try:
            self.report(job, result, time.monotonic() - job.accepted_at)
        except Exception:  # a failed report (database full...) mustn't stop chat's input
            log.exception("reporting a job failed")


async def _after[T](seconds: float, job: Coroutine[Any, Any, T]) -> T:
    if seconds > 0:
        await asyncio.sleep(seconds)
    return await job


def _timeout(command: InputCommand) -> float:
    if isinstance(command, ClickText):
        return RESOLVE_TIMEOUT + JOB_TIMEOUT
    steps = len(command.steps) if isinstance(command, MacroRun) else 1
    return JOB_TIMEOUT * max(1, steps)


async def execute(vnc: VncInput, command: InputCommand, grid: Grid) -> None:
    """Send one command's input events. Grid coordinates are scaled to the server's screen."""

    def scale(x: int, y: int) -> tuple[int, int]:
        return x * vnc.width // grid.width, y * vnc.height // grid.height

    match command:
        case TypeText(text=text):
            for ch in text:
                keysym = char_keysym(ch)
                if keysym is None:  # the parser lets none through; never send a guess
                    continue
                await vnc.key(keysym, True)
                await vnc.key(keysym, False)
                await asyncio.sleep(KEY_DELAY)
        case PressKeys(combos=combos, repeat=repeat):
            for _ in range(repeat):
                for combo in combos:
                    await vnc.combo(combo)
                    await asyncio.sleep(KEY_DELAY)
        case Click(x=x, y=y, button=button, double=double):
            await vnc.click(*scale(x, y), BUTTONS[button], 2 if double else 1)
        case Move(x=x, y=y):
            await vnc.pointer(*scale(x, y))
        case Drag(start=start, end=end):
            await vnc.drag(scale(*start), scale(*end))
        case Scroll(direction=direction, amount=amount):
            await vnc.scroll(direction, amount)
        case MacroRun(steps=steps):
            for i, step in enumerate(steps):
                if i:
                    await asyncio.sleep(MACRO_GAP)
                await execute(vnc, step, grid)
        case ClickText():  # Executor resolves it first; never send a guess
            pass


async def connect_to(host: str, port: int = 5900) -> VncInput:
    vnc = VncInput(host, port)
    await vnc.connect()
    return vnc
