#!/usr/bin/env python3
"""Create the chat-uses Discord roles and channels (PLAN.md, Phase 0).

Idempotent: roles and channels that already exist (matched by name, type and
category) are left alone, so it is safe to re-run. It never deletes anything.

Usage:
  # 1. Print the invite link, open it, add the bot to your server:
  setup_server.py invite-url --application-id 123456789012345678

  # 2. Preview, then create:
  DISCORD_BOT_TOKEN=... setup_server.py apply --guild-id 987654321098765432 --dry-run
  DISCORD_BOT_TOKEN=... setup_server.py apply --guild-id 987654321098765432

The token can also come from a file (--token-file), e.g.
/etc/chat-uses/secrets/discord-bot-token. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API_BASE = os.environ.get("DISCORD_API_BASE", "https://discord.com/api/v10")
USER_AGENT = "DiscordBot (https://github.com/wblondel/chat-uses, 0.1)"

# --- permissions (https://discord.com/developers/docs/topics/permissions) ---

KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
MANAGE_CHANNELS = 1 << 4
ADD_REACTIONS = 1 << 6
VIEW_AUDIT_LOG = 1 << 7
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16
CONNECT = 1 << 20
SPEAK = 1 << 21
MUTE_MEMBERS = 1 << 22
MOVE_MEMBERS = 1 << 24
MANAGE_NICKNAMES = 1 << 27
MANAGE_ROLES = 1 << 28
REQUEST_TO_SPEAK = 1 << 32
MANAGE_THREADS = 1 << 34
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
SEND_MESSAGES_IN_THREADS = 1 << 38
MODERATE_MEMBERS = 1 << 40

MODERATOR_PERMS = (
    KICK_MEMBERS | BAN_MEMBERS | VIEW_AUDIT_LOG | MANAGE_MESSAGES | MANAGE_THREADS
    | MODERATE_MEMBERS | MUTE_MEMBERS | MOVE_MEMBERS | MANAGE_NICKNAMES
)
BOT_POST = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | READ_MESSAGE_HISTORY
NO_POSTING = SEND_MESSAGES | SEND_MESSAGES_IN_THREADS | CREATE_PUBLIC_THREADS | CREATE_PRIVATE_THREADS
STAGE_MODERATION = MANAGE_CHANNELS | MUTE_MEMBERS | MOVE_MEMBERS

# Everything the bot needs: setup, runtime (moderation, posting, audio relay),
# and every permission it grants in roles or overwrites (Discord requires that).
BOT_PERMS = (
    MANAGE_ROLES | MANAGE_CHANNELS | BOT_POST | ADD_REACTIONS | SEND_MESSAGES_IN_THREADS
    | CONNECT | SPEAK | REQUEST_TO_SPEAK | MODERATOR_PERMS | NO_POSTING
)

ROLE = 0
MEMBER = 1

TEXT = 0
CATEGORY = 4
STAGE = 13

# --- desired layout ----------------------------------------------------------


@dataclass
class Role:
    name: str
    color: int
    hoist: bool
    permissions: int = 0


ROLES = [
    Role("Owner", 0xE67E22, hoist=True),
    Role("Moderator", 0x2ECC71, hoist=True, permissions=MODERATOR_PERMS),
    Role("Regular", 0x3498DB, hoist=False),
]


@dataclass
class Channel:
    name: str
    type: int = TEXT
    topic: str = ""
    readonly: bool = False  # everyone reads, moderators and the bot post
    slowmode: int = 0


@dataclass
class Category:
    name: str
    channels: list[Channel] = field(default_factory=list)
    private: bool = False  # only moderators and the bot can see it


LAYOUT = [
    Category("📢 Info", [
        Channel("announcements", topic="Stream schedule and news", readonly=True),
        Channel("rules", topic="Rules for chat and for the machine", readonly=True),
    ]),
    Category("🕹️ Play", [
        Channel("control", topic="Send commands to the machine: !type, !click, !key… See #rules.", slowmode=2),
        Channel("general", topic="Talk about anything"),
        Channel("theorycraft", topic="Plans, puzzles and conspiracies"),
        Channel("📻 Machine audio", type=STAGE),
    ]),
    Category("📜 Records", [
        Channel("graveyard", topic="Every dead run, posted by the bot", readonly=True),
        Channel("achievements", topic="Achievements as they unlock", readonly=True),
        Channel("clips", topic="Approved clips from the stream", readonly=True),
    ]),
    Category("🔒 Staff", private=True, channels=[
        Channel("mod-alerts", topic="Content holds, panics and reports"),
        Channel("ops", topic="Stream and server health alerts"),
    ]),
]

# --- API client --------------------------------------------------------------


class DiscordError(RuntimeError):
    pass


class Discord:
    def __init__(self, token: str, base: str = API_BASE, dry_run: bool = False):
        self.token = token
        self.base = base.rstrip("/")
        self.dry_run = dry_run
        self._fake_id = 0

    def request(self, method: str, path: str, body: dict | None = None):
        if self.dry_run and method != "GET":
            self._fake_id += 1
            return {"id": f"dry-run-{self._fake_id}", **(body or {})}
        data = json.dumps(body).encode() if body is not None else None
        for _ in range(6):
            req = urllib.request.Request(self.base + path, data=data, method=method)
            req.add_header("Authorization", f"Bot {self.token}")
            req.add_header("User-Agent", USER_AGENT)
            req.add_header("X-Audit-Log-Reason", "chat-uses setup")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as err:
                with err:  # the error wraps an open response; close it
                    payload = err.read().decode(errors="replace")
                if err.code == 429:
                    try:
                        wait = float(json.loads(payload).get("retry_after", 1))
                    except ValueError:
                        wait = 1.0
                    time.sleep(min(wait, 60) + 0.1)
                    continue
                hint = ""
                if err.code == 401:
                    hint = " (invalid bot token)"
                elif err.code == 403:
                    hint = " (the bot lacks a permission: re-invite it with the invite-url link)"
                raise DiscordError(f"{method} {path} → HTTP {err.code}{hint}: {payload}") from None
        raise DiscordError(f"{method} {path}: still rate limited after several retries")


# --- planning helpers --------------------------------------------------------


def overwrite(target_id: str, kind: int, allow: int = 0, deny: int = 0) -> dict:
    return {"id": target_id, "type": kind, "allow": str(allow), "deny": str(deny)}


def channel_overwrites(guild_id: str, bot_id: str, roles: dict[str, str],
                       category: Category, channel: Channel | None) -> list[dict]:
    """Permission overwrites for a category (channel=None) or a channel in it."""
    everyone = guild_id  # the @everyone role has the guild's id
    mods = roles["Moderator"]
    if category.private:
        return [
            overwrite(everyone, ROLE, deny=VIEW_CHANNEL),
            overwrite(mods, ROLE, allow=VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY),
            overwrite(bot_id, MEMBER, allow=BOT_POST),
        ]
    if channel is None:
        return []
    if channel.type == STAGE:
        return [
            overwrite(everyone, ROLE, allow=VIEW_CHANNEL | CONNECT, deny=SPEAK | REQUEST_TO_SPEAK),
            overwrite(mods, ROLE, allow=STAGE_MODERATION | SPEAK),
            overwrite(bot_id, MEMBER, allow=VIEW_CHANNEL | CONNECT | SPEAK | STAGE_MODERATION),
        ]
    if channel.readonly:
        return [
            overwrite(everyone, ROLE, deny=NO_POSTING),
            overwrite(mods, ROLE, allow=SEND_MESSAGES),
            overwrite(bot_id, MEMBER, allow=BOT_POST),
        ]
    return []


def normalize(overwrites: list[dict]) -> list[tuple]:
    return sorted((o["id"], int(o["type"]), str(o["allow"]), str(o["deny"])) for o in overwrites)


def apply(api: Discord, guild_id: str, sync_permissions: bool, out=sys.stdout) -> dict:
    """Create missing roles and channels. Returns counts for reporting/tests."""
    stats = {"roles_created": 0, "channels_created": 0, "updated": 0, "skipped": 0}

    def say(msg: str) -> None:
        print(msg, file=out)

    me = api.request("GET", "/users/@me")
    guild = api.request("GET", f"/guilds/{guild_id}")
    community = "COMMUNITY" in guild.get("features", [])
    say(f"Server: {guild['name']} (bot: {me['username']}{', dry run' if api.dry_run else ''})")

    existing_roles = {r["name"]: r for r in api.request("GET", f"/guilds/{guild_id}/roles")}
    role_ids: dict[str, str] = {}
    for role in ROLES:
        found = existing_roles.get(role.name)
        if found:
            role_ids[role.name] = found["id"]
            if sync_permissions and int(found["permissions"]) != role.permissions:
                api.request("PATCH", f"/guilds/{guild_id}/roles/{found['id']}",
                            {"permissions": str(role.permissions)})
                stats["updated"] += 1
                say(f"  role {role.name}: permissions updated")
            else:
                say(f"  role {role.name}: exists")
            continue
        created = api.request("POST", f"/guilds/{guild_id}/roles", {
            "name": role.name, "color": role.color, "hoist": role.hoist,
            "mentionable": False, "permissions": str(role.permissions),
        })
        role_ids[role.name] = created["id"]
        stats["roles_created"] += 1
        say(f"  role {role.name}: created")

    channels = api.request("GET", f"/guilds/{guild_id}/channels")

    def find(name: str, kind: int, parent: str | None):
        for ch in channels:
            if ch["name"] == name and ch["type"] == kind and ch.get("parent_id") == parent:
                return ch
        return None

    def ensure(name: str, kind: int, parent: str | None, extra: dict, overwrites: list[dict]) -> str:
        found = find(name, kind, parent)
        label = f"{'category' if kind == CATEGORY else 'channel'} {name}"
        if found:
            if sync_permissions and normalize(found.get("permission_overwrites", [])) != normalize(overwrites):
                api.request("PATCH", f"/channels/{found['id']}", {"permission_overwrites": overwrites})
                stats["updated"] += 1
                say(f"  {label}: permissions updated")
            else:
                say(f"  {label}: exists")
            return found["id"]
        body = {"name": name, "type": kind, "permission_overwrites": overwrites, **extra}
        if parent:
            body["parent_id"] = parent
        created = api.request("POST", f"/guilds/{guild_id}/channels", body)
        channels.append({**body, "id": created["id"], "parent_id": parent})
        stats["channels_created"] += 1
        say(f"  {label}: created")
        return created["id"]

    for category in LAYOUT:
        cat_id = ensure(category.name, CATEGORY, None, {},
                        channel_overwrites(guild_id, me["id"], role_ids, category, None))
        for channel in category.channels:
            if channel.type == STAGE and not community:
                stats["skipped"] += 1
                say(f"  channel {channel.name}: SKIPPED (stage channels need Community enabled: "
                    "Server Settings → Enable Community, then re-run)")
                continue
            extra: dict = {}
            if channel.topic:
                extra["topic"] = channel.topic
            if channel.slowmode:
                extra["rate_limit_per_user"] = channel.slowmode
            ensure(channel.name, channel.type, cat_id, extra,
                   channel_overwrites(guild_id, me["id"], role_ids, category, channel))

    say(f"Done: {stats['roles_created']} roles and {stats['channels_created']} channels created, "
        f"{stats['updated']} updated, {stats['skipped']} skipped.")
    return stats


def invite_url(application_id: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": application_id,
        "scope": "bot applications.commands",
        "permissions": str(BOT_PERMS),
    })
    return f"https://discord.com/oauth2/authorize?{query}"


def read_token(path: str | None) -> str:
    if path:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    else:
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("error: set DISCORD_BOT_TOKEN or pass --token-file")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_invite = sub.add_parser("invite-url", help="print the link that adds the bot with the right permissions")
    p_invite.add_argument("--application-id", required=True, help="Developer Portal → your app → Application ID")

    p_apply = sub.add_parser("apply", help="create missing roles and channels")
    p_apply.add_argument("--guild-id", required=True, help="right-click the server icon → Copy Server ID")
    p_apply.add_argument("--token-file", help="file containing the bot token (default: $DISCORD_BOT_TOKEN)")
    p_apply.add_argument("--dry-run", action="store_true", help="show what would be created, change nothing")
    p_apply.add_argument("--sync-permissions", action="store_true",
                         help="also reset permissions of existing roles/channels to this layout")

    args = parser.parse_args(argv)
    if args.command == "invite-url":
        print(invite_url(args.application_id))
        return 0

    api = Discord(read_token(args.token_file), dry_run=args.dry_run)
    try:
        apply(api, args.guild_id, args.sync_permissions)
    except DiscordError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
