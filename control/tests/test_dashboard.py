import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp import test_utils
from fake_incus import FakeTarget

from chatuses.brain import Brain
from chatuses.executor import connect_to
from chatuses.incus import Incus
from chatuses.store import Store

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


@asynccontextmanager
async def dashboard(
    token: str | None = TOKEN, incus: Incus | None = None
) -> AsyncGenerator[tuple[Brain, test_utils.TestClient[Any, Any]]]:
    with Store(":memory:") as store:
        brain = Brain(store, lambda: connect_to("127.0.0.1", 1), incus)
        async with test_utils.TestClient(test_utils.TestServer(brain.web_app(token))) as client:
            yield brain, client


def test_every_api_call_needs_the_token() -> None:
    async def test() -> None:
        async with dashboard() as (brain, client):
            for method, path in [("GET", "/api/state"), ("POST", "/api/panic"), ("GET", "/api/commands")]:
                for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]:
                    resp = await client.request(method, path, headers=headers)
                    assert resp.status == 401, (method, path, headers)
                    assert resp.headers["WWW-Authenticate"].startswith("Bearer")
            assert not brain.moderation.panicking
            resp = await client.get("/api/state", headers=AUTH)
            assert resp.status == 200 and resp.headers["Cache-Control"] == "no-store"
            assert "Access-Control-Allow-Origin" not in resp.headers

    run(test)


def test_the_page_holds_no_secret_and_locks_itself_down() -> None:
    async def test() -> None:
        async with dashboard() as (_brain, client):
            resp = await client.get("/admin", allow_redirects=False)
            assert resp.status == 302 and resp.headers["Location"] == "/admin/"
            page = await client.get("/admin/")
            assert page.status == 200 and page.content_type == "text/html"
            assert TOKEN not in await page.text()
            csp = page.headers["Content-Security-Policy"]
            assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
            for path, kind in [("/admin/admin.js", "text/javascript"), ("/admin/admin.css", "text/css")]:
                resp = await client.get(path)
                assert resp.status == 200 and resp.content_type == kind
            assert (await client.get("/admin/../brain.py")).status == 404

    run(test)


def test_no_token_no_dashboard() -> None:
    async def test() -> None:
        async with dashboard(token=None) as (_brain, client):
            assert (await client.get("/api/state", headers=AUTH)).status == 404
            assert (await client.get("/admin/")).status == 404
            assert (await client.get("/healthz")).status == 200

    run(test)


def test_panic_and_resume_from_the_dashboard() -> None:
    async def test() -> None:
        async with dashboard() as (brain, client):
            headers = {**AUTH, "X-Moderator": "Sam\tLee"}
            resp = await client.post("/api/panic", json={"reason": "gore"}, headers=headers)
            assert (await resp.json())["ok"] is True
            assert brain.moderation.panic_state is not None
            assert (
                brain.moderation.panic_state["by"] == "SamLee on the dashboard"
            )  # control characters dropped
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert state["panic"]["reason"] == "gore" and "PANIC since" in state["status"]
            assert (await client.post("/api/panic", headers=AUTH)).status == 200  # no body is fine
            resp = await client.post("/api/resume", headers=AUTH)
            assert (await resp.json())["ok"] is True and not brain.moderation.panicking
            assert brain.store.recent_events(("resume",))[0][3]["by"] == "the dashboard"

    run(test)


def test_bans_lockdown_and_bad_requests() -> None:
    async def test() -> None:
        async with dashboard() as (brain, client):
            brain.store.seen_user("yt:a", "Alice", time.time())

            async def post(path: str, body: Any) -> tuple[int, Any]:
                resp = await client.post(path, json=body, headers=AUTH)
                return resp.status, (await resp.json() if resp.status == 200 else await resp.text())

            status, answer = await post("/api/ban", {"who": "Alice", "duration": "1d", "reason": "x"})
            assert status == 200 and answer["ok"]
            assert (await post("/api/ban", {"who": "Alice", "duration": "soon"}))[1]["ok"] is False
            assert (await post("/api/ban", {"who": "", "duration": "1d"}))[0] == 400
            assert (await post("/api/ban", ["Alice"]))[0] == 400
            assert (await post("/api/timeout", {"who": "yt:b", "minutes": 5}))[1]["ok"]
            assert (await post("/api/timeout", {"who": "yt:b", "minutes": "5"}))[0] == 400
            assert (await post("/api/timeout", {"who": "yt:b", "minutes": True}))[0] == 400
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert {b["user"] for b in state["banned"]} == {"yt:a", "yt:b"}
            assert (await post("/api/unban", {"who": "yt:a"}))[1]["ok"]
            assert (await post("/api/lockdown", {"on": True}))[1]["ok"] and brain.moderation.locked
            assert (await post("/api/lockdown", {"on": "yes"}))[0] == 400
            resp = await client.post("/api/lockdown", data="{not json", headers=AUTH)
            assert resp.status == 400

    run(test)


def test_the_vote_and_ending_it() -> None:
    async def test() -> None:
        async with dashboard(incus=FakeTarget()) as (brain, client):
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert state["referee"]["ruleset"] == "roguelike" and state["referee"]["vote"] is None
            assert "power_votes" in state["referee"]["features"]
            assert "achievements" in state["referee"]["not_built_yet"]
            now = time.time()
            brain.pipeline.presence.saw("console:alice", now - 600)
            await brain.console_message("alice", "!reboot")
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert (state["referee"]["vote"]["text"], state["referee"]["vote"]["phase"]) == (
                "!reboot",
                "voting",
            )
            answer = await (await client.post("/api/endvote", json={}, headers=AUTH)).json()
            assert answer == {"ok": True, "text": "Vote ended: !reboot (ended by a moderator)."}
            answer = await (await client.post("/api/endvote", json={}, headers=AUTH)).json()
            assert answer == {"ok": False, "text": "No vote is running."}
            [(_, _, _, payload)] = brain.store.recent_events(("endvote",))
            assert payload == {"by": "the dashboard"}

    run(test)


def test_the_audit_and_the_moderation_log() -> None:
    async def test() -> None:
        async with dashboard() as (brain, client):
            for i, user in enumerate(["alice", "bob", "alice"]):
                await brain.console_message(user, f"!click Z{i}")  # invalid: recorded with the reason
            body = await (await client.get("/api/commands", headers=AUTH)).json()
            rows = body["commands"]
            assert [r["raw"] for r in rows] == ["!click Z2", "!click Z1", "!click Z0"]
            assert (
                rows[0]["user"] == "console:alice" and rows[0]["outcome"] == "invalid" and rows[0]["reason"]
            )
            older = await client.get(f"/api/commands?before={rows[0]['id']}&limit=1", headers=AUTH)
            assert [r["raw"] for r in (await older.json())["commands"]] == ["!click Z1"]
            mine = await client.get("/api/commands?user=console:bob", headers=AUTH)
            assert [r["raw"] for r in (await mine.json())["commands"]] == ["!click Z1"]
            assert (await client.get("/api/commands?limit=lots", headers=AUTH)).status == 400

            brain.moderation.ban("m", "console:bob", "1h")
            brain.moderation.lockdown("m", True)
            events = (await (await client.get("/api/events", headers=AUTH)).json())["events"]
            assert [e["type"] for e in events] == ["lockdown", "ban"]  # moderation only, newest first
            assert events[1]["user"] == "console:bob" and events[1]["by"] == "m"
            everything = await client.get("/api/events?types=all", headers=AUTH)
            assert "command" in {e["type"] for e in (await everything.json())["events"]}

            found = await (await client.get("/api/users?q=ALI", headers=AUTH)).json()
            assert [u["id"] for u in found["users"]] == ["console:alice"]
            assert found["users"][0]["shown_as"] == "alice" and found["users"][0]["banned"] is False

    run(test)


def test_screenshots_need_the_token_and_a_proper_name(tmp_path: Path) -> None:
    async def test() -> None:
        async with dashboard() as (brain, client):
            assert (await client.get("/api/screens/ocr-20261116-101010.png", headers=AUTH)).status == 404
            brain.screens_dir = tmp_path
            (tmp_path / "ocr-20261116-101010.png").write_bytes(b"\x89PNG fake")
            (tmp_path / "secret.txt").write_text("no")
            assert (await client.get("/api/screens/ocr-20261116-101010.png")).status == 401
            resp = await client.get("/api/screens/ocr-20261116-101010.png", headers=AUTH)
            assert resp.status == 200 and resp.content_type == "image/png"
            assert await resp.read() == b"\x89PNG fake"
            for bad in ["secret.txt", "..%2Fsecret.txt", "ocr-1.png", "ocr-20261116-101011.png"]:
                assert (await client.get(f"/api/screens/{bad}", headers=AUTH)).status == 404, bad
            brain.store.event(1.0, "ocr_hit", {"mode": "shadow", "found": ["x"], "screenshot": "ocr-x.png"})
            events = (await (await client.get("/api/events", headers=AUTH)).json())["events"]
            assert events[0]["type"] == "ocr_hit"
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert state["ocr"] == {"mode": "off"}

    run(test)


def test_snapshots_and_rewinding_from_the_dashboard() -> None:
    async def test() -> None:
        target = FakeTarget()
        async with dashboard(incus=target) as (brain, client):
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert state["time_travel"] == []
            assert brain.snapshots is not None
            snap = await brain.snapshots.take("auto")
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert state["time_travel"] == [
                {"id": snap.id, "kind": "auto", "label": snap.label, "created": snap.created, "thumb": False}
            ]
            headers = {**AUTH, "X-Moderator": "Mia"}
            resp = await client.post("/api/rewind-to", json={"which": str(snap.id)}, headers=headers)
            assert await resp.json() == {"ok": True, "text": f"Rewinding to #{snap.id} ({snap.label})…"}
            await asyncio.sleep(0.05)
            assert target.restored == [snap.ref]
            [(_, _, _, payload)] = brain.store.recent_events(("rewind-to",))
            assert payload == {"by": "Mia on the dashboard", "snapshot": str(snap.id)}
            resp = await client.post("/api/rewind-to", json={"which": ""}, headers=AUTH)
            assert resp.status == 400

    run(test)


def test_without_incus_the_dashboard_says_time_travel_is_off() -> None:
    async def test() -> None:
        async with dashboard() as (_brain, client):
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert state["time_travel"] is None

    run(test)


def test_switching_the_mode_from_the_dashboard() -> None:
    async def test() -> None:
        async with dashboard() as (brain, client):
            state = await (await client.get("/api/state", headers=AUTH)).json()
            assert (state["referee"]["mode"]["mode"], state["referee"]["mode"]["meter"]) == ("anarchy", 50)
            resp = await client.post("/api/mode", json={"mode": "democracy"}, headers=AUTH)
            assert await resp.json() == {"ok": True, "text": "Mode: democracy (locked for 120 s)."}
            assert brain.referee.democracy
            assert (await client.post("/api/mode", json={"mode": "chaos"}, headers=AUTH)).status == 400
            [(_, _, _, payload)] = brain.store.recent_events(("mode",))
            assert payload == {"mode": "democracy", "by": "the dashboard"}

    run(test)
