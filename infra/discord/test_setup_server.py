"""Tests for setup_server.py against an in-memory fake of the Discord API.

Run: python3 -m unittest discover -s infra/discord
"""

from __future__ import annotations

import io
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import setup_server as s

GUILD = "1000"
BOT = "2000"


class FakeDiscord:
    def __init__(self, community: bool = True):
        self.features = ["COMMUNITY"] if community else []
        self.roles = [{"id": GUILD, "name": "@everyone", "permissions": "0"}]
        self.channels: list[dict] = []
        self.writes: list[tuple[str, str]] = []
        self.next_id = 5000
        self.rate_limit_next_write = False
        self.auth_headers: set[str] = set()

    def new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def handle(self, method: str, path: str, body: dict | None):
        if method != "GET":
            if self.rate_limit_next_write:
                self.rate_limit_next_write = False
                return 429, {"message": "You are being rate limited.", "retry_after": 0.01}
            self.writes.append((method, path))
        if path == "/users/@me":
            return 200, {"id": BOT, "username": "chat-uses-bot"}
        if path == f"/guilds/{GUILD}":
            return 200, {"id": GUILD, "name": "Test server", "features": self.features}
        if path == f"/guilds/{GUILD}/roles":
            if method == "GET":
                return 200, self.roles
            role = {"id": self.new_id(), **body}
            self.roles.append(role)
            return 200, role
        if m := re.fullmatch(rf"/guilds/{GUILD}/roles/(\d+)", path):
            role = next(r for r in self.roles if r["id"] == m[1])
            role.update(body)
            return 200, role
        if path == f"/guilds/{GUILD}/channels":
            if method == "GET":
                return 200, self.channels
            channel = {"id": self.new_id(), "parent_id": None, **body}
            self.channels.append(channel)
            return 201, channel
        if m := re.fullmatch(r"/channels/(\d+)", path):
            channel = next(c for c in self.channels if c["id"] == m[1])
            channel.update(body)
            return 200, channel
        return 404, {"message": "Unknown route"}


def serve(fake: FakeDiscord):
    class Handler(BaseHTTPRequestHandler):
        def _do(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            fake.auth_headers.add(self.headers.get("Authorization", ""))
            path = self.path.removeprefix("/api/v10")
            code, payload = fake.handle(self.command, path, body)
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = _do

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/api/v10"


class SetupServerTest(unittest.TestCase):
    def run_apply(self, fake: FakeDiscord, dry_run=False, sync=False):
        server, base = serve(fake)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)  # cleanups run last-in, first-out
        api = s.Discord("test-token", base=base, dry_run=dry_run)
        return s.apply(api, GUILD, sync_permissions=sync, out=io.StringIO())

    def expected_channels(self, community=True):
        n = 0
        for cat in s.LAYOUT:
            n += 1
            n += sum(1 for ch in cat.channels if community or ch.type != s.STAGE)
        return n

    def test_first_run_creates_everything_second_run_nothing(self):
        fake = FakeDiscord()
        first = self.run_apply(fake)
        self.assertEqual(first["roles_created"], len(s.ROLES))
        self.assertEqual(first["channels_created"], self.expected_channels())
        self.assertEqual(fake.auth_headers, {"Bot test-token"})

        writes_before = len(fake.writes)
        second = self.run_apply(fake)
        self.assertEqual(second["roles_created"], 0)
        self.assertEqual(second["channels_created"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(len(fake.writes), writes_before)

    def test_dry_run_writes_nothing(self):
        fake = FakeDiscord()
        stats = self.run_apply(fake, dry_run=True)
        self.assertEqual(fake.writes, [])
        self.assertEqual(stats["channels_created"], self.expected_channels())

    def test_stage_skipped_without_community(self):
        fake = FakeDiscord(community=False)
        stats = self.run_apply(fake)
        self.assertEqual(stats["skipped"], 1)
        self.assertFalse(any(c["type"] == s.STAGE for c in fake.channels))

    def test_rate_limit_is_retried(self):
        fake = FakeDiscord()
        fake.rate_limit_next_write = True
        stats = self.run_apply(fake)
        self.assertEqual(stats["roles_created"], len(s.ROLES))

    def test_channels_are_nested_and_staff_is_private(self):
        fake = FakeDiscord()
        self.run_apply(fake)
        by_name = {c["name"]: c for c in fake.channels}
        staff = by_name["🔒 Staff"]
        self.assertEqual(by_name["mod-alerts"]["parent_id"], staff["id"])
        everyone = next(o for o in by_name["mod-alerts"]["permission_overwrites"] if o["id"] == GUILD)
        self.assertTrue(int(everyone["deny"]) & s.VIEW_CHANNEL)
        bot = next(o for o in by_name["mod-alerts"]["permission_overwrites"] if o["id"] == BOT)
        self.assertEqual(bot["type"], s.MEMBER)
        self.assertEqual(by_name["control"]["rate_limit_per_user"], 2)
        graveyard_everyone = next(o for o in by_name["graveyard"]["permission_overwrites"] if o["id"] == GUILD)
        self.assertTrue(int(graveyard_everyone["deny"]) & s.SEND_MESSAGES)

    def test_sync_permissions_repairs_drift(self):
        fake = FakeDiscord()
        self.run_apply(fake)
        control = next(c for c in fake.channels if c["name"] == "graveyard")
        control["permission_overwrites"] = []
        mods = next(r for r in fake.roles if r["name"] == "Moderator")
        mods["permissions"] = "0"
        stats = self.run_apply(fake, sync=True)
        self.assertEqual(stats["updated"], 2)
        self.assertEqual(self.run_apply(fake, sync=True)["updated"], 0)

    def test_bot_has_every_permission_it_grants(self):
        granted = s.MODERATOR_PERMS
        for cat in s.LAYOUT:
            for ch in [None, *cat.channels]:
                roles = {"Moderator": "3"}
                for o in s.channel_overwrites(GUILD, BOT, roles, cat, ch):
                    granted |= int(o["allow"]) | int(o["deny"])
        self.assertEqual(granted & ~s.BOT_PERMS, 0)

    def test_invite_url(self):
        url = s.invite_url("42")
        self.assertIn("client_id=42", url)
        self.assertIn(f"permissions={s.BOT_PERMS}", url)
        self.assertIn("scope=bot+applications.commands", url)


if __name__ == "__main__":
    unittest.main()
