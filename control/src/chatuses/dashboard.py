"""The admin dashboard and its API (PLAN.md 7.5, 7.6): /admin and /api/*.

Behind a token (ADMIN_TOKEN_FILE): every API call needs
`Authorization: Bearer <token>`. The page holds no secret: it asks for the
token and keeps it in the browser tab (session storage). Moderators reach it
over the WireGuard admin tunnel (http://10.20.0.11:8080/admin). bcast and the
other infrastructure containers can reach the port too (D37), hence the token.
There are no cookies, so no cross-site requests, and no CORS headers, so
other pages can't read the answers.

    GET  /api/state                          panic, lockdown, bans, machine and chat status
    POST /api/panic     {"reason"}           also for a hotkey or a Stream Deck (curl)
    POST /api/resume
    POST /api/lockdown  {"on"}
    POST /api/ban       {"who", "duration", "reason"}
    POST /api/timeout   {"who", "minutes"}
    POST /api/unban     {"who"}
    GET  /api/users?q=                       find someone by name or ID
    GET  /api/commands?user=&before=&limit=  the audit: who sent what, and what became of it
    GET  /api/events?types=&before=&limit=   moderation's log (types=all: everything)
    GET  /api/screens/<name>                 a screenshot screen OCR kept (PNG)
    POST /api/ocr-verdict {"hit", "verdict"} a screen OCR hit was "real" or "false" (Phase 7)

Actions answer {"ok": bool, "text": "..."}, as Discord's slash commands do.
`X-Moderator: <name>` (the page sends what you typed) goes in the log as
who did it; it is the moderator's word, not proven by the shared token.
"""

import hmac
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from chatuses.safety.moderation import NAME_LOOKBACK, Answer

if TYPE_CHECKING:
    from chatuses.brain import Brain

log = logging.getLogger(__name__)

PAGE_DIR = Path(__file__).parent / "admin_page"
PAGE_FILES = {"": "index.html", "admin.js": "admin.js", "admin.css": "admin.css"}
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
MODERATION_EVENTS = ("panic", "hold", "resume", "ban", "timeout", "unban", "lockdown", "snapshot", "ocr_hit")
SCREEN_NAME = re.compile(r"ocr-\d{8}-\d{6}\.png")
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
MAX_LIMIT = 500

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def add_routes(app: web.Application, brain: "Brain", token: str) -> None:
    api = _Api(brain, token)
    for name, file in PAGE_FILES.items():
        app.router.add_get(f"/admin/{name}", _page(file))
    app.router.add_get("/admin", _redirect)
    app.router.add_get("/api/state", api.guard(api.state))
    app.router.add_post("/api/panic", api.guard(api.panic))
    app.router.add_post("/api/resume", api.guard(api.resume))
    app.router.add_post("/api/lockdown", api.guard(api.lockdown))
    app.router.add_post("/api/ban", api.guard(api.ban))
    app.router.add_post("/api/timeout", api.guard(api.timeout))
    app.router.add_post("/api/unban", api.guard(api.unban))
    app.router.add_post("/api/endvote", api.guard(api.endvote))
    app.router.add_post("/api/rewind-to", api.guard(api.rewind_to))
    app.router.add_post("/api/mode", api.guard(api.mode))
    app.router.add_get("/api/users", api.guard(api.users))
    app.router.add_get("/api/commands", api.guard(api.commands))
    app.router.add_get("/api/events", api.guard(api.events))
    app.router.add_get("/api/screens/{name}", api.guard(api.screen))
    app.router.add_post("/api/ocr-verdict", api.guard(api.ocr_verdict))


async def _redirect(_request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/admin/")


def _page(file: str) -> Handler:
    path = PAGE_DIR / file
    content_type = CONTENT_TYPES[path.suffix]

    async def serve(_request: web.Request) -> web.StreamResponse:
        return web.Response(body=path.read_bytes(), content_type=content_type, headers=PAGE_HEADERS)

    return serve


def _int(value: str | None, default: int | None, low: int, high: int) -> int | None:
    if value is None or value == "":
        return default
    if not value.isdigit():
        raise web.HTTPBadRequest(text="not a number\n")
    return max(low, min(high, int(value)))


class _Api:
    def __init__(self, brain: "Brain", token: str) -> None:
        self.brain = brain
        self.mod = brain.moderation
        self._token = token.encode()
        self._last_refused = 0.0

    def guard(self, handler: Handler) -> Handler:
        async def guarded(request: web.Request) -> web.StreamResponse:
            given = request.headers.get("Authorization", "")
            scheme, _, secret = given.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(secret.strip().encode(), self._token):
                now = time.monotonic()
                if now - self._last_refused > 10:
                    self._last_refused = now
                    log.warning(
                        "admin API: refused a request without the right token from %s", request.remote
                    )
                raise web.HTTPUnauthorized(
                    text="token needed\n", headers={"WWW-Authenticate": 'Bearer realm="chat-uses"'}
                )
            response = await handler(request)
            response.headers.update(API_HEADERS)
            return response

        return guarded

    @staticmethod
    def _by(request: web.Request) -> str:
        name = request.headers.get("X-Moderator", "").strip()[:40]
        name = "".join(ch for ch in name if ch.isprintable())
        return f"{name} on the dashboard" if name else "the dashboard"

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            body: object = json.loads(await request.text())
        except ValueError as err:
            raise web.HTTPBadRequest(text="JSON, please\n") from err
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="a JSON object, please\n")
        return {str(k): v for k, v in body.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]

    @staticmethod
    def _text(body: dict[str, Any], key: str, required: bool = True) -> str:
        value = body.get(key, "")
        if not isinstance(value, str) or (required and not value.strip()):
            raise web.HTTPBadRequest(text=f"{key}: text, please\n")
        return value

    @staticmethod
    def _answer(answer: Answer) -> web.Response:
        return web.json_response({"ok": answer.ok, "text": answer.text})

    async def state(self, _request: web.Request) -> web.StreamResponse:
        brain = self.brain
        return web.json_response(
            {
                **self.mod.state(),
                "target": brain.executor.online.is_set(),
                "waiting": brain.executor.queue.qsize(),
                "overlay_pages": brain.overlay.clients,
                "chat": brain.adapters(),
                "snapshots": brain.incus is not None,  # forensic, on panic
                "time_travel": [
                    {
                        "id": s.id,
                        "kind": s.kind,
                        "label": s.label,
                        "created": s.created,
                        "thumb": bool(s.thumb),
                    }
                    for s in brain.snapshots.all()
                ]
                if brain.snapshots
                else None,
                "ocr": {"mode": brain.ocr_mode, **brain.watch.status()} if brain.watch else {"mode": "off"},
                "watchdog": brain.watchdog.status(),
                "referee": brain.referee.status(),
                "status": brain.status_text(),
            }
        )

    async def panic(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        return self._answer(self.mod.panic(self._by(request), self._text(body, "reason", required=False)))

    async def resume(self, request: web.Request) -> web.StreamResponse:
        return self._answer(self.mod.resume(self._by(request)))

    async def lockdown(self, request: web.Request) -> web.StreamResponse:
        on = (await self._body(request)).get("on")
        if not isinstance(on, bool):
            raise web.HTTPBadRequest(text="on: true or false\n")
        return self._answer(self.mod.lockdown(self._by(request), on))

    async def ban(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        who, duration = self._text(body, "who"), self._text(body, "duration")
        return self._answer(
            self.mod.ban(self._by(request), who, duration, self._text(body, "reason", required=False))
        )

    async def timeout(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        minutes = body.get("minutes", 10)
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            raise web.HTTPBadRequest(text="minutes: a whole number\n")
        return self._answer(self.mod.timeout(self._by(request), self._text(body, "who"), minutes))

    async def unban(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        return self._answer(self.mod.unban(self._by(request), self._text(body, "who")))

    async def mode(self, request: web.Request) -> web.StreamResponse:
        mode = (await self._body(request)).get("mode")
        if mode not in ("anarchy", "democracy"):
            raise web.HTTPBadRequest(text="mode: anarchy or democracy\n")
        return self._answer(self.mod.mode(self._by(request), str(mode)))

    async def rewind_to(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        return self._answer(self.mod.rewind_to(self._by(request), self._text(body, "which")))

    async def endvote(self, request: web.Request) -> web.StreamResponse:
        return self._answer(self.mod.endvote(self._by(request)))

    async def ocr_verdict(self, request: web.Request) -> web.StreamResponse:
        body = await self._body(request)
        hit, verdict = body.get("hit"), body.get("verdict")
        if not isinstance(hit, int) or isinstance(hit, bool) or verdict not in ("real", "false"):
            raise web.HTTPBadRequest(text='hit: a number; verdict: "real" or "false"\n')
        return self._answer(self.mod.verdict(self._by(request), hit, str(verdict)))

    async def users(self, request: web.Request) -> web.StreamResponse:
        query = request.query.get("q", "").strip().casefold()
        now = time.time()
        found: list[dict[str, Any]] = []
        for user in self.brain.store.users_seen_since(now - NAME_LOOKBACK):
            shown = self.brain.filter.safe_name(user["id"], user["display_name"])
            if (
                query in user["id"].casefold()
                or query in user["display_name"].casefold()
                or query in shown.casefold()
            ):
                found.append({**user, "shown_as": shown, "banned": user["banned_until"] > now})
            if len(found) >= 50:
                break
        return web.json_response({"users": found})

    async def commands(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        rows = self.brain.store.recent_commands(
            before_id=_int(q.get("before"), None, 0, 2**62),
            user_id=q.get("user") or None,
            limit=_int(q.get("limit"), 100, 1, MAX_LIMIT) or 100,
        )
        return web.json_response({"commands": rows})

    async def events(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        types = q.get("types", "")
        wanted = () if types == "all" else tuple(t for t in types.split(",") if t) or MODERATION_EVENTS
        rows = self.brain.store.recent_events(
            wanted,
            before_id=_int(q.get("before"), None, 0, 2**62),
            limit=_int(q.get("limit"), 100, 1, MAX_LIMIT) or 100,
        )
        verdicts = self.brain.store.ocr_verdicts([i for i, _ts, t, _p in rows if t == "ocr_hit"])
        events: list[dict[str, Any]] = []
        for i, ts, t, payload in rows:
            event = {"id": i, "ts": ts, "type": t, **payload}
            if t == "ocr_hit":
                event["verdict"] = verdicts.get(i)
            events.append(event)
        return web.json_response({"events": events})

    async def screen(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        folder = self.brain.screens_dir
        if folder is None or not SCREEN_NAME.fullmatch(name) or not (folder / name).is_file():
            raise web.HTTPNotFound(text="no such screenshot\n")
        return web.Response(body=(folder / name).read_bytes(), content_type="image/png")
