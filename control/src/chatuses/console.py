"""Console: talk to brain from a shell in its container.

Console chat: a message typed here goes through exactly what a chat message
goes through (pipeline, rate limits, executor, overlay), as user
"console:<name>". For testing, and for playing without YouTube or Discord.
Admin requests set what brain takes from its operator (admin.py).

It listens on a Unix socket (only root and brain's own user can open it),
never on the network. One request per connection:

    client: {"name": "alice", "text": "!type hello"}\\n     or  {"admin": ["youtube", "VIDEO"]}\\n
    brain:  {"outcome": "accepted", "reason": null}\\n

    chat-brain-say [--as NAME] MESSAGE...     (python -m chatuses.console)
    chat-brain-admin ARGS...                  (python -m chatuses.admin)
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

MAX_REQUEST = 4096  # bytes
TIMEOUT = 5.0  # seconds

# Called with (name, text); returns the outcome kind and the reason shown to chat.
Handler = Callable[[str, str], Awaitable[tuple[str, str | None]]]
# Called with the admin request's words; returns "ok" or "error", and a text for the operator.
AdminHandler = Callable[[list[str]], Awaitable[tuple[str, str]]]


async def no_admin(_args: list[str]) -> tuple[str, str]:
    return "error", "no admin requests here"


async def serve(path: Path, handle: Handler, admin: AdminHandler = no_admin) -> asyncio.Server:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), TIMEOUT)
            request = _object(json.loads(line[:MAX_REQUEST]))
            if "admin" in request:
                outcome, reason = await admin(_strings(request["admin"]))
            else:
                name, text = _string(request["name"]), _string(request["text"])
                outcome, reason = await handle(name, text)
            reply = {"outcome": outcome, "reason": reason}
        except (TimeoutError, ValueError, KeyError, TypeError) as err:
            reply = {"outcome": "error", "reason": f"bad request: {type(err).__name__}"}
        with contextlib.suppress(ConnectionError):
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(client, path, limit=MAX_REQUEST)
    os.chmod(path, 0o600)
    return server


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("not an object")
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("not a string")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return [_string(item) for item in cast(list[object], value)]


async def request(path: Path, payload: dict[str, object]) -> dict[str, str | None]:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), TIMEOUT)
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    reply = json.loads(await asyncio.wait_for(reader.readline(), TIMEOUT))
    writer.close()
    return reply


async def say(path: Path, name: str, text: str) -> dict[str, str | None]:
    return await request(path, {"name": name, "text": text})


def main() -> None:
    args = argparse.ArgumentParser(description="Send a chat message to brain, as a console user.")
    args.add_argument("--as", dest="name", default="console", help="user name shown on stream")
    args.add_argument("--socket", default=os.environ.get("BRAIN_CONSOLE", "/run/chat-brain/console.sock"))
    args.add_argument("--pause", type=float, default=1.0, help="seconds between messages (default 1)")
    args.add_argument("message", nargs="+", help="the message; several are sent one after another")
    opts = args.parse_args()
    failed = False
    for i, text in enumerate(opts.message):
        if i:
            time.sleep(opts.pause)
        try:
            reply = asyncio.run(say(Path(opts.socket), opts.name, text))
        except (OSError, TimeoutError, ValueError) as err:
            sys.exit(f"can't reach brain at {opts.socket}: {err or type(err).__name__}")
        reason = f": {reply['reason']}" if reply.get("reason") else ""
        print(f"{text}  ->  {reply['outcome']}{reason}")
        failed |= reply["outcome"] not in ("accepted", "chat")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
