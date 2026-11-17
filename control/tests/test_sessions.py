import asyncio
import time
from pathlib import Path
from typing import Any

from chatuses.brain import Brain
from chatuses.executor import connect_to
from chatuses.identity import Presence
from chatuses.sessions import Sessions, active_by_platform, build_report, render
from chatuses.store import Store

T = 1_800_000_000.0  # 2027-01-15 08:00 UTC


class Clock:
    def __init__(self, now: float = T) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_a_session_starts_samples_ends_and_outlives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    clock = Clock()
    with Store(db) as store:
        sessions = Sessions(store, clock)
        assert sessions.end() == (False, "No session running.", None)
        assert not sessions.start("   ")[0]
        assert sessions.start("Beta  1")[0]
        assert sessions.start("Beta 2") == (False, "Session 'Beta 1' is running: end it first (session end).")
        clock.now += 60
        sessions.sample(12, {"yt": 3, "dc": 1})
    with Store(db) as store:  # brain restarts mid-session
        sessions = Sessions(store, clock)
        assert sessions.status() == "Session 'Beta 1': 1 min, 1 samples."
        clock.now += 60
        sessions.sample(None, {"yt": 1})  # YouTube didn't say
        clock.now += 60
        ok, text, report = sessions.end()
        assert ok and report is not None and text.startswith("📊 Session 'Beta 1': 3 min")
        assert (report["viewers"], report["active"]["peak"], report["samples"]) == (
            {"peak": 12, "average": 12.0},
            4,
            2,
        )
        assert sessions.current is None
        [row] = store.sessions()
        assert (row["name"], row["ended"] - row["started"], row["report"]) == ("Beta 1", 180, report)
        kinds = [kind for _id, _ts, kind, _payload in store.events()]
        assert kinds == ["session", "session"]


def numbers(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "outcomes": {"accepted": 90, "rate_limited": 6, "invalid": 4},
        "people": {"yt": 7, "dc": 3},
        "votes": [("rewind", "done"), ("rewind", "failed"), ("save", "done"), ("reboot", "vetoed")],
        "events": [
            ("panic", {"by": "Mia", "reason": "x"}),
            ("resume", {"seconds": 90, "kind": "panic"}),
            ("hold", {"by": "screen OCR"}),
            ("resume", {"seconds": 30, "kind": "hold"}),
            ("hold", {"by": "screen OCR"}),
            ("panic", {"by": "Mia", "from_hold": True}),
            ("resume", {"seconds": 120, "kind": "panic"}),
            ("ocr_hit", {"mode": "enforce"}),
            ("ocr_hit", {"mode": "enforce"}),
            ("ocr_hit", {"mode": "shadow"}),
            ("time-travel", {}),
            ("power", {"action": "reboot"}),
        ],
        "ocr_verdicts": {"real": 2, "false": 1},
    }
    return base | changes


def test_the_report_adds_up_the_session() -> None:
    samples = [[T + 60, 10, {"yt": 4, "dc": 2}], [T + 120, 20, {"yt": 8, "dc": 1}], [T + 180, 30, {"dc": 3}]]
    report = build_report("Beta 1", T, T + 50 * 60, samples, numbers())
    assert report["viewers"] == {"peak": 30, "average": 20.0}
    assert report["active"] == {"peak": 9, "average": 6.0, "peaks": {"dc": 3, "yt": 8}}
    assert report["commands"] == {
        "total": 100,
        "per_minute": 2.0,
        "accepted": 90,
        "refused": {"rate_limited": 6, "invalid": 4},
    }
    assert report["votes"] == {
        "proposed": 4,
        "done": 2,
        "failed": 1,
        "vetoed": 1,
        "by_kind": {"reboot": [1, 0], "rewind": [2, 1], "save": [1, 1]},
    }
    assert (report["panics"], report["holds"]) == (1, {"count": 2, "confirmed": 1, "resumed": 1})
    assert report["covered_minutes"] == 4.0  # 90 + 30 + 120 s
    assert report["ocr_hits"] == {"count": 3, "modes": {"enforce": 2, "shadow": 1}, "real": 2, "false": 1}
    assert (report["time_travel"], report["power"]) == (1, 1)
    assert render(report).split("\n") == [
        "📊 Session 'Beta 1': 50 min (2027-01-15, 08:00 UTC to 08:50 UTC)",
        "Viewers (YouTube): peak 30, average 20.0",
        "Active in chat: peak 9, average 6.0 (Discord 3, YouTube 8 at their peaks)",
        "People who sent commands: 10 (Discord 3, YouTube 7)",
        "Commands: 100 (2.0 a minute): 90 accepted, 10 refused (rate_limited 6, invalid 4)",
        "Votes: 4 proposed, 2 carried out (reboot 0 of 1, rewind 1 of 2, save 1 of 1); 1 failed, 1 vetoed",
        "Panics: 1; content holds: 2 (1 confirmed as panics, 1 false alarm resumed); covered 4.0 min in all",
        "Screen OCR hits: 3 (2 real, 1 false alarm, 0 not marked)",
        "Time travel: 1, reboots and resets: 1",
    ]


def test_an_empty_session_reads_well() -> None:
    report = build_report("Quiet", T, T + 125 * 60, [], numbers(outcomes={}, people={}, votes=[], events=[]))
    text = render(report)
    assert "2 h 05 min" in text and "Viewers (YouTube): in YouTube Studio" in text
    assert "Commands: 0 (0.0 a minute): 0 accepted, 0 refused" in text and "Votes: none" in text


def test_people_active_by_platform() -> None:
    presence = Presence()
    for user in ("yt:a", "yt:b", "dc:c", "console:d"):
        presence.saw(user, T - 10)
    presence.saw("yt:gone", T - 600)  # quiet for 10 minutes
    assert active_by_platform(presence, T) == {"console": 1, "dc": 1, "yt": 2}


def test_sessions_from_chat_brain_admin_count_the_session_and_post_in_ops() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            posted: list[str] = []
            brain.ops = posted.append  # type: ignore[method-assign]
            assert await brain.admin(["sessions"]) == ("ok", "No sessions yet.")
            assert (await brain.admin(["session", "start", "Beta", "1"]))[0] == "ok"
            assert "Session 'Beta 1'" in brain.status_text()
            await brain.console_message("alice", "!help")
            await brain.console_message("alice", "!nope")
            await brain.console_message("bob", "!help")
            brain.sessions.sample(None, {"console": 2})
            ok, text = await brain.admin(["session", "end"])
            assert ok == "ok" and posted == [text]
            assert "People who sent commands: 2 (console 2)" in text
            assert "Commands: 3" in text and "2 accepted, 1 refused (invalid 1)" in text
            assert (await brain.admin(["session", "end"])) == ("error", "No session running.")
            ok, listed = await brain.admin(["sessions"])
            assert ok == "ok" and listed.startswith("#1 'Beta 1', ") and "3 commands, 0 votes" in listed

    asyncio.run(test())


def test_samples_wait_for_a_session(monkeypatch: Any) -> None:
    from chatuses import brain as brain_module

    monkeypatch.setattr(brain_module, "SAMPLE_EVERY", 0.01)

    async def test() -> None:
        with Store(":memory:") as store:
            brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
            task = asyncio.create_task(brain._session_samples())  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0.05)
            assert store.sessions() == []
            brain.sessions.start("Beta")
            brain.pipeline.presence.saw("yt:a", time.time())
            await asyncio.sleep(0.05)
            running = brain.sessions.current
            assert (
                running is not None and running["samples"] and running["samples"][-1][1:] == [None, {"yt": 1}]
            )
            task.cancel()

    asyncio.run(test())
