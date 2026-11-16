import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import test_utils

from chatuses.brain import Brain
from chatuses.executor import connect_to
from chatuses.store import Store

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


@asynccontextmanager
async def dashboard(
    token: str | None = TOKEN,
) -> AsyncGenerator[tuple[Brain, test_utils.TestClient[Any, Any]]]:
    with Store(":memory:") as store:
        brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
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
