"""The watchdog (PLAN.md 6.7, Phase 5): is the target healthy, and if one of
the desktop's services died, start it again.

Every CHECK seconds:
- **the instance runs** (Incus API). Stopped or frozen: an alert in #ops,
  nothing more. An operator may have stopped it, and what a dead target means
  for the game is Phase 6's (runs, death votes, revives).
- **the desktop's services are active** (`systemctl is-active` through Incus
  exec, every SERVICES seconds). One that isn't is restarted. If systemctl
  itself can't run (`rm -rf /` from chat: red-team test 10), the target's
  system is destroyed: #ops hears it once, and nothing is restarted, since
  nothing could be. Bringing it back is a human's job until Phase 6's revive.
- **brain's VNC link is up.** Down for more than VNC_GRACE seconds while the
  services say they're fine: the display and the session are restarted.
- **the screen moves after input.** If commands reached the target and the
  screen (as screen OCR reads it) didn't change in the FROZEN seconds after,
  #ops hears that it looks frozen. Nothing is restarted for that: a still
  screen can be fine, and a reboot is Phase 6's decision.

A unit is restarted at most RESTARTS times in RESTART_WINDOW seconds; after
that it's left to a human, with one alert. A check that fails (Incus
unreachable, a refusal) stays a problem until a whole check goes through
again, and #ops hears it once it has lasted BLIND seconds. Without Incus
access only the VNC and screen checks run, and they only alert.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import aiohttp

from chatuses.incus import Incus, IncusError

log = logging.getLogger(__name__)

CHECK = 10.0  # seconds between checks
SERVICES = 30.0  # seconds between service checks (each is an exec in the target)
VNC_GRACE = 60.0  # seconds VNC may be down before the display is restarted
FROZEN = 120.0  # seconds after input without a screen change
RESTARTS = 3  # per unit...
RESTART_WINDOW = 900.0  # ...in this many seconds
BLIND = 60.0  # seconds the target can't be checked before #ops hears it
CANT_RUN = (126, 127)  # systemctl's exit when it can't be run at all
UNITS = ("chat-xvnc", "chat-session", "chat-audio", "chat-audio-pcm")
DISPLAY_UNITS = ("chat-xvnc", "chat-session")


class Watchdog:
    def __init__(
        self,
        incus: Incus | None,
        address: str,
        instance: str,
        vnc_up: Callable[[], bool],
        last_input: Callable[[], float],
        last_change: Callable[[], float | None],
        alert: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        units: tuple[str, ...] = UNITS,
    ) -> None:
        self.incus = incus
        self.address = address
        self.instance = instance  # found by address on first use if empty
        self.vnc_up = vnc_up
        self.last_input = last_input
        self.last_change = last_change
        self.alert = alert
        self.clock = clock
        self.units = units
        self.instance_state = "unknown"
        self.services: dict[str, str] = {}
        self.screen = "unknown"  # ok, frozen, unknown (no screen OCR)
        self.vnc_down_since: float | None = None
        self.destroyed = False  # running, but systemctl can't run in it
        self.last_error = ""
        self._error_since: float | None = None
        self._services_checked = 0.0
        self._restarts: dict[str, deque[float]] = {}
        self._gave_up: set[str] = set()
        self._said: set[str] = set()  # alerts already sent for an ongoing problem

    def status(self) -> dict[str, Any]:
        problems = self.problems()
        return {
            "state": "healthy" if not problems else "unhealthy",
            "problems": problems,
            "instance": self.instance or None,
            "instance_state": self.instance_state,
            "services": dict(self.services),
            "screen": self.screen,
            "last_error": self.last_error,
        }

    def problems(self) -> list[str]:
        found: list[str] = []
        if self.instance_state not in ("Running", "unknown"):
            found.append(f"the target is {self.instance_state.lower()}")
        if self.destroyed:
            found.append("the target's system is destroyed (systemctl can't run)")
        found += [f"{unit} is {state}" for unit, state in self.services.items() if state != "active"]
        if self.vnc_down_since is not None:
            found.append("VNC is down")
        if self.screen == "frozen":
            found.append("the screen looks frozen")
        if self.last_error:
            found.append(f"can't check the target: {self.last_error}")
        return found

    async def run(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(CHECK)

    async def check(self) -> None:
        now = self.clock()
        try:
            await self._check_target(now)
        except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
            self._failed(now, str(err) or type(err).__name__)
        self._check_vnc(now)
        self._check_screen(now)

    def _failed(self, now: float, error: str) -> None:
        """A check that didn't go through: it stays a problem until one does."""
        self.last_error = error
        log.warning("watchdog: %s", error)
        if self._error_since is None:
            self._error_since = now
        elif now - self._error_since > BLIND:
            self._once("blind", f"🙈 The watchdog can't check the target: {error}.")

    def _went_through(self) -> None:
        self.last_error = ""
        self._error_since = None
        self._said.discard("blind")

    async def _check_target(self, now: float) -> None:
        if self.incus is None:
            return
        if not self.instance:
            self.instance = await self.incus.find(self.address)
        self.instance_state = await self.incus.state(self.instance)
        if self.instance_state != "Running":
            self.services = {}
            self.destroyed = False
            self._went_through()
            self._once("instance", f"🪦 The target ({self.instance}) is {self.instance_state.lower()}.")
            return
        self._said.discard("instance")
        # After a failed check, every check is a whole one until one goes through.
        if self.last_error or now - self._services_checked >= SERVICES:
            self._services_checked = now
            await self._check_services(now)
            self._went_through()
        if (
            self.vnc_down_since is not None
            and now - self.vnc_down_since > VNC_GRACE
            and not self.destroyed
            and all(state == "active" for state in self.services.values())
        ):
            for unit in DISPLAY_UNITS:
                await self._restart(unit, now, f"VNC was down for {round(now - self.vnc_down_since)} s")
            self.vnc_down_since = now  # give the restart its own grace

    async def _check_services(self, now: float) -> None:
        assert self.incus is not None
        services: dict[str, str] = {}
        for unit in self.units:
            code = await self.incus.exec(self.instance, ["systemctl", "is-active", "--quiet", unit])
            if code in CANT_RUN:
                self.services, self.destroyed = {}, True
                self._once(
                    "destroyed",
                    f"🪦 The target ({self.instance}) is running, but systemctl can't run in it "
                    f"(exit {code}): its system looks destroyed (files deleted?). Nothing to restart: "
                    "it needs rebuilding by hand.",
                )
                return
            services[unit] = "active" if code == 0 else "down"
        self.services, self.destroyed = services, False
        self._said.discard("destroyed")
        for unit, state in self.services.items():
            if state != "active":
                await self._restart(unit, now, f"it was {state}")

    async def _restart(self, unit: str, now: float, why: str) -> None:
        assert self.incus is not None
        recent = self._restarts.setdefault(unit, deque())
        while recent and now - recent[0] > RESTART_WINDOW:
            recent.popleft()
        if len(recent) >= RESTARTS:
            if unit not in self._gave_up:
                self._gave_up.add(unit)
                self.alert(
                    f"🧯 {unit} on {self.instance} keeps failing ({RESTARTS} restarts in "
                    f"{round(RESTART_WINDOW / 60)} minutes): leaving it to a human."
                )
            return
        recent.append(now)
        self._gave_up.discard(unit)
        code = await self.incus.exec(self.instance, ["systemctl", "restart", unit])
        result = "restarted" if code == 0 else f"tried to restart (exit {code})"
        log.warning("watchdog: %s %s on %s: %s", result, unit, self.instance, why)
        self.alert(f"🔧 Watchdog {result} {unit} on {self.instance}: {why}.")

    def _check_vnc(self, now: float) -> None:
        if self.vnc_up():
            self.vnc_down_since = None
            self._said.discard("vnc")
            return
        if self.vnc_down_since is None:
            self.vnc_down_since = now
        elif now - self.vnc_down_since > VNC_GRACE:
            self._once(
                "vnc", f"📵 brain can't reach the target's VNC (for {round(now - self.vnc_down_since)} s)."
            )

    def _check_screen(self, now: float) -> None:
        changed, sent = self.last_change(), self.last_input()
        if changed is None:
            self.screen = "unknown"
            return
        if sent and changed < sent and now - sent > FROZEN:
            if self.screen != "frozen":
                self.screen = "frozen"
                self.alert(
                    f"🧊 The target's screen hasn't changed in the {round(now - sent)} s since the last "
                    "command reached it: it may be frozen."
                )
        else:
            self.screen = "ok"

    def _once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.alert(text)
