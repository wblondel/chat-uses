import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from chatuses.incus import Incus, IncusError
from chatuses.safety import watchdog as watchdog_module
from chatuses.safety.watchdog import Watchdog


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


class FakeIncus(Incus):
    def __init__(self) -> None:  # no certificates needed
        self.status = "Running"
        self.down: set[str] = set()
        self.restarts: list[str] = []
        self.restart_fails = False
        self.error: Exception | None = None

    async def find(self, address: str) -> str:
        return "desk"

    async def state(self, instance: str) -> str:
        if self.error:
            raise self.error
        return self.status

    async def exec(self, instance: str, command: list[str]) -> int:
        if command[:2] == ["systemctl", "is-active"]:
            return 3 if command[-1] in self.down else 0
        if command[:2] == ["systemctl", "restart"]:
            self.restarts.append(command[-1])
            if not self.restart_fails:
                self.down.discard(command[-1])
            return 1 if self.restart_fails else 0
        raise AssertionError(command)


class Clock:
    now = 10_000.0

    def __call__(self) -> float:
        return self.now


def dog(
    incus: Incus | None = None, vnc: list[bool] | None = None, clock: Clock | None = None
) -> tuple[Watchdog, list[str], dict[str, Any]]:
    alerts: list[str] = []
    state: dict[str, Any] = {"input": 0.0, "change": None}
    up = vnc if vnc is not None else [True]
    w = Watchdog(
        incus,
        "10.30.0.251",
        "",
        vnc_up=lambda: up[0],
        last_input=lambda: state["input"],
        last_change=lambda: state["change"],
        alert=alerts.append,
        clock=clock or Clock(),
    )
    return w, alerts, state


def test_a_healthy_target() -> None:
    async def test() -> None:
        w, alerts, _ = dog(FakeIncus())
        await w.check()
        assert w.instance == "desk" and w.status()["state"] == "healthy"
        assert w.services == dict.fromkeys(watchdog_module.UNITS, "active")
        assert alerts == []

    run(test)


def test_a_dead_service_is_restarted_then_left_to_a_human() -> None:
    async def test() -> None:
        incus, clock = FakeIncus(), Clock()
        incus.down = {"chat-audio"}
        w, alerts, _ = dog(incus, clock=clock)
        await w.check()
        assert incus.restarts == ["chat-audio"]
        assert alerts == ["🔧 Watchdog restarted chat-audio on desk: it was down."]
        incus.restart_fails = True
        for _ in range(4):
            incus.down = {"chat-audio"}
            clock.now += watchdog_module.SERVICES
            await w.check()
        assert incus.restarts.count("chat-audio") == watchdog_module.RESTARTS  # no more than that
        assert "keeps failing" in alerts[-1] and sum("keeps failing" in a for a in alerts) == 1
        assert "chat-audio is down" in w.status()["problems"]
        clock.now += watchdog_module.RESTART_WINDOW  # later: it may try again
        await w.check()
        assert incus.restarts.count("chat-audio") == watchdog_module.RESTARTS + 1

    run(test)


def test_services_are_checked_every_30_seconds_not_every_check() -> None:
    async def test() -> None:
        incus, clock = FakeIncus(), Clock()
        w, _, _ = dog(incus, clock=clock)
        await w.check()
        incus.down = {"chat-xvnc"}
        clock.now += 10
        await w.check()
        assert incus.restarts == []
        clock.now += 20
        await w.check()
        assert incus.restarts == ["chat-xvnc"]

    run(test)


def test_a_stopped_target_is_reported_not_started() -> None:
    async def test() -> None:
        incus = FakeIncus()
        incus.status = "Stopped"
        w, alerts, _ = dog(incus)
        await w.check()
        await w.check()
        assert alerts == ["🪦 The target (desk) is stopped."]  # once
        assert incus.restarts == [] and "the target is stopped" in w.problems()
        incus.status = "Running"
        await w.check()
        assert w.status()["state"] == "healthy"

    run(test)


def test_vnc_down_while_services_look_fine_restarts_the_display() -> None:
    async def test() -> None:
        incus, clock, vnc = FakeIncus(), Clock(), [False]
        w, alerts, _ = dog(incus, vnc, clock)
        await w.check()
        assert incus.restarts == [] and "VNC is down" in w.problems()
        clock.now += watchdog_module.VNC_GRACE + 1
        await w.check()
        assert incus.restarts == ["chat-xvnc", "chat-session"]
        assert alerts == [
            "🔧 Watchdog restarted chat-xvnc on desk: VNC was down for 61 s.",
            "🔧 Watchdog restarted chat-session on desk: VNC was down for 61 s.",
        ]
        vnc[0] = True
        await w.check()
        assert "VNC is down" not in w.problems()

    run(test)


def test_a_screen_that_doesnt_move_after_input_is_reported() -> None:
    async def test() -> None:
        clock = Clock()
        w, alerts, state = dog(None, clock=clock)
        await w.check()
        assert w.screen == "unknown"  # no screen OCR, no idea
        state["change"], state["input"] = clock.now - 10, clock.now
        clock.now += 60
        await w.check()
        assert w.screen == "ok"  # not long enough yet
        clock.now += watchdog_module.FROZEN
        await w.check()
        await w.check()
        assert w.screen == "frozen" and len(alerts) == 1 and "may be frozen" in alerts[0]
        state["change"] = clock.now
        await w.check()
        assert w.screen == "ok"

    run(test)


def test_incus_trouble_is_noted_and_the_other_checks_go_on() -> None:
    async def test() -> None:
        incus = FakeIncus()
        incus.error = IncusError("not authorized")
        w, _, _ = dog(incus, [False])
        await w.check()
        assert w.last_error == "not authorized" and w.vnc_down_since is not None
        assert "can't check the target: not authorized" in w.status()["problems"]

    run(test)


def test_without_incus_vnc_trouble_is_only_reported() -> None:
    async def test() -> None:
        clock = Clock()
        w, alerts, _ = dog(None, [False], clock)
        await w.check()
        clock.now += watchdog_module.VNC_GRACE + 1
        await w.check()
        await w.check()
        assert len(alerts) == 1 and "can't reach the target's VNC (for 61 s)" in alerts[0]

    run(test)
