import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from fake_vnc import FakeVnc, Key, Pointer

from chatuses import executor as executor_module
from chatuses import vnc as vnc_module
from chatuses.commands import InputCommand, TypeText
from chatuses.executor import Executor, Job, JobResult, connect_to, execute, record_in
from chatuses.grid import Grid
from chatuses.parser import parse
from chatuses.store import Store


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def command(message: str) -> InputCommand:
    parsed = parse(message, Grid())
    assert isinstance(parsed, InputCommand), parsed
    return parsed


async def events_for(message: str, width: int = 1600, height: int = 900) -> list[Key | Pointer]:
    server = await FakeVnc(width=width, height=height).start()
    vnc = await connect_to("127.0.0.1", server.port)
    await execute(vnc, command(message), Grid())
    await vnc.close()
    events = list(await server.wait_hung_up())
    await server.stop()
    return events


def keys(text: str) -> list[Key]:
    return [("key", down, ord(ch)) for ch in text for down in (True, False)]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("!type Hé!", keys("Hé!")),  # the server adds Shift; é is its own keysym
        ("!enter", [("key", True, 0xFF0D), ("key", False, 0xFF0D)]),
        ("!bksp 2", [("key", d, 0xFF08) for _ in range(2) for d in (True, False)]),
        ("!click F7", [("ptr", 0, 550, 650), ("ptr", 1, 550, 650), ("ptr", 0, 550, 650)]),
        ("!rclick A1", [("ptr", 0, 50, 50), ("ptr", 4, 50, 50), ("ptr", 0, 50, 50)]),
        ("!move P9", [("ptr", 0, 1550, 850)]),
    ],
)
def test_commands_become_input_events(message: str, expected: list[Key | Pointer]) -> None:
    async def test() -> None:
        assert await events_for(message) == expected

    run(test)


def test_grid_coordinates_follow_the_real_screen_size() -> None:
    async def test() -> None:
        # Half the size: F7 (550, 650) lands at (275, 325).
        assert await events_for("!click F7", 800, 450) == [
            ("ptr", 0, 275, 325),
            ("ptr", 1, 275, 325),
            ("ptr", 0, 275, 325),
        ]

    run(test)


def test_drag_holds_the_button_all_the_way() -> None:
    async def test() -> None:
        events = await events_for("!drag B2 H6")
        assert events[0] == ("ptr", 0, 150, 150) and events[-1] == ("ptr", 0, 750, 550)
        assert all(e[1] == 1 for e in events[1:-1])

    run(test)


# --- the executor -----------------------------------------------------------

Reports = list[tuple[str, JobResult]]


def reporter(reports: Reports) -> executor_module.Report:
    return lambda job, result, _seconds: reports.append((job.user_id, result))


async def online(executor: Executor, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        await executor.online.wait()


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_module, "RECONNECT", (0.01, 0.05))


def test_jobs_run_in_order_and_are_reported() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        reports: Reports = []
        executor = Executor(lambda: connect_to("127.0.0.1", server.port), report=reporter(reports))
        task = asyncio.create_task(executor.run())
        assert not executor.submit(Job("yt:early", TypeText("x")))  # not connected yet
        await online(executor)
        assert executor.submit(Job("yt:a", TypeText("ab")))
        assert executor.submit(Job("yt:b", TypeText("c")))
        await until(lambda: len(reports) == 2)
        assert reports == [("yt:a", "done"), ("yt:b", "done")]
        assert await server.wait_for(6) == keys("abc")
        task.cancel()
        await server.stop()

    run(test)


def test_a_full_queue_refuses_more() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        executor = Executor(lambda: connect_to("127.0.0.1", server.port), max_pending=2)
        task = asyncio.create_task(executor.run())
        await online(executor)
        results = [executor.submit(Job("yt:a", TypeText("x"))) for _ in range(3)]
        assert results == [True, True, False]
        task.cancel()
        await server.stop()

    run(test)


def test_losing_the_target_drops_what_was_waiting_and_reconnects() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        reports: Reports = []
        executor = Executor(lambda: connect_to("127.0.0.1", server.port), report=reporter(reports))
        task = asyncio.create_task(executor.run())
        await online(executor)
        for user in ("yt:a", "yt:b", "yt:c"):
            assert executor.submit(Job(user, TypeText("x" * 40)))
        await server.wait_for(1)  # a's typing has started
        await server.hang_up()
        await until(lambda: len(reports) == 3)
        assert reports == [("yt:a", "offline"), ("yt:b", "offline"), ("yt:c", "offline")]
        await online(executor)  # back, on a new connection
        assert server.connections == 2
        assert executor.submit(Job("yt:d", TypeText("y")))
        await until(lambda: len(reports) == 4)
        assert reports[-1] == ("yt:d", "done")
        task.cancel()
        await server.stop()

    run(test)


def test_a_hang_up_while_idle_is_noticed_at_once() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        await server.stop()  # nobody listening: the executor keeps retrying
        executor = Executor(lambda: connect_to("127.0.0.1", server.port))
        task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.1)
        assert not executor.online.is_set()
        await server.start()  # same port
        await online(executor)
        await server.hang_up()
        await until(lambda: server.connections == 2)  # reconnected with nothing submitted
        await online(executor)
        task.cancel()
        await server.stop()

    run(test)


def test_a_stuck_command_times_out_and_lets_go_of_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_module, "JOB_TIMEOUT", 0.05)

    async def test() -> None:
        server = await FakeVnc().start()
        reports: Reports = []
        executor = Executor(lambda: connect_to("127.0.0.1", server.port), report=reporter(reports))
        task = asyncio.create_task(executor.run())
        await online(executor)
        executor.submit(Job("yt:a", command("!key shift+a shift+b shift+c shift+d")))
        executor.submit(Job("yt:a", TypeText("x" * 40)))  # 40 keys take about 0.3 s
        await until(lambda: len(reports) == 2)
        assert reports[1] == ("yt:a", "offline")
        await server.wait_hung_up()  # everything sent on the first connection has arrived
        held: dict[int, bool] = {}
        for event in server.events:
            if event[0] == "key":
                held[event[2]] = event[1]
        assert not any(held.values())  # every key that went down came back up
        task.cancel()
        await server.stop()

    run(test)


def test_executed_events_are_recorded() -> None:
    with Store(":memory:") as store:
        record_in(store)(Job("yt:a", TypeText("hi"), accepted_at=0.0), "done", 0.1234)
        [(_, _, kind, payload)] = store.events()
    assert kind == "executed"
    assert (payload["user"], payload["result"], payload["ms"]) == ("yt:a", "done", 123)
    assert '"TypeText"' in payload["command"]


def test_a_failing_report_does_not_stop_the_executor(caplog: pytest.LogCaptureFixture) -> None:
    def broken(_job: Job, _result: JobResult, _seconds: float) -> None:
        raise OSError("database or disk is full")

    async def test() -> None:
        server = await FakeVnc().start()
        executor = Executor(lambda: connect_to("127.0.0.1", server.port), report=broken)
        task = asyncio.create_task(executor.run())
        await online(executor)
        assert executor.submit(Job("yt:a", TypeText("a")))
        assert executor.submit(Job("yt:b", TypeText("b")))
        assert await server.wait_for(4) == keys("ab")
        task.cancel()
        await server.stop()

    run(test)
    assert "reporting a job failed" in caplog.text


def test_stopping_the_executor_leaves_nothing_open() -> None:
    async def test() -> None:
        server = await FakeVnc().start()
        executor = Executor(lambda: connect_to("127.0.0.1", server.port))
        task = asyncio.create_task(executor.run())
        await online(executor)  # now waiting for a job
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.wait_hung_up()
        await server.stop()

    run(test)


# --- panic: freeze and thaw ---------------------------------------------------


def test_freezing_stops_the_running_command_lets_go_and_drops_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vnc_module, "DRAG_STEP", 0.2)  # a 2-second drag, stopped half-way

    async def test() -> None:
        server = await FakeVnc().start()
        reports: Reports = []
        executor = Executor(lambda: connect_to("127.0.0.1", server.port), report=reporter(reports))
        task = asyncio.create_task(executor.run())
        await online(executor)
        assert executor.submit(Job("yt:dragger", command("!drag B2 H6")))
        assert executor.submit(Job("yt:waiting", TypeText("x")))
        await server.wait_for(2)  # the button is down
        executor.freeze()
        await until(lambda: len(reports) == 2)
        assert sorted(reports) == [("yt:dragger", "flushed"), ("yt:waiting", "flushed")]
        await asyncio.sleep(0.3)
        events = list(server.events)
        assert events[-1][:2] == ("ptr", 0)  # let go
        assert len(events) < 13  # the drag didn't finish
        assert not executor.can_take() and not executor.submit(Job("yt:a", TypeText("x")))
        assert executor.online.is_set()  # still connected: resume is instant
        executor.thaw()
        assert executor.submit(Job("yt:a", TypeText("y")))
        await until(lambda: len(reports) == 3)
        assert reports[-1] == ("yt:a", "done")
        task.cancel()
        await server.stop()

    run(test)


def test_cancelling_the_executor_mid_command_still_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vnc_module, "DRAG_STEP", 0.2)

    async def test() -> None:
        server = await FakeVnc().start()
        executor = Executor(lambda: connect_to("127.0.0.1", server.port))
        task = asyncio.create_task(executor.run())
        await online(executor)
        executor.submit(Job("yt:a", command("!drag B2 H6")))
        await server.wait_for(2)
        task.cancel()  # brain stopping, not a panic
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await server.wait_hung_up()
        assert events[-1][:2] == ("ptr", 0)  # closing let go of the button
        await server.stop()

    run(test)
