"""Drive a desktop over VNC with chat commands, from the command line.

    python -m chatuses.drive 10.30.0.251 '!key ctrl+alt+t' '!wait 2' '!type echo hi' '!enter'

Each argument goes through the real parser and executor, in order, with a
short pause between them. `!wait N` (seconds, here only) waits longer. For
the sandbox end-to-end test and for poking at a target by hand; brain
itself doesn't use this.
"""

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from chatuses.commands import InputCommand, ParseError
from chatuses.executor import execute
from chatuses.grid import Grid
from chatuses.parser import parse
from chatuses.vnc import VncError, VncInput


@dataclass(frozen=True)
class Wait:
    seconds: float


def plan(messages: list[str], grid: Grid) -> list[InputCommand | Wait]:
    """Commands and waits, or SystemExit naming the first bad argument."""
    steps: list[InputCommand | Wait] = []
    for i, message in enumerate(messages, 1):
        word, _, rest = message.partition(" ")
        if word == "!wait":
            try:
                steps.append(Wait(float(rest)))
            except ValueError:
                raise SystemExit(f"argument {i}: !wait takes seconds") from None
            continue
        parsed = parse(message, grid)
        if parsed is None or isinstance(parsed, ParseError):
            reason = parsed.reason if parsed else "not a command"
            raise SystemExit(f"argument {i}: {reason}")
        if not isinstance(parsed, InputCommand):
            raise SystemExit(f"argument {i}: only input commands can be driven")
        steps.append(parsed)
    return steps


async def drive(host: str, port: int, steps: list[InputCommand | Wait], pause: float) -> None:
    vnc = VncInput(host, port)
    await vnc.connect()
    print(f"connected: {vnc.width}x{vnc.height}, desktop {vnc.name[:64]!r}")
    grid = Grid()
    try:
        for step in steps:
            if isinstance(step, Wait):
                await asyncio.sleep(step.seconds)
                continue
            start = time.monotonic()
            await execute(vnc, step, grid)
            print(f"{type(step).__name__}: {(time.monotonic() - start) * 1000:.0f} ms")
            await asyncio.sleep(pause)
    finally:
        await vnc.close()


def main() -> None:
    args = argparse.ArgumentParser(description="Drive a desktop over VNC with chat commands.")
    args.add_argument("host", help="VNC server, HOST or HOST:PORT (default port 5900)")
    args.add_argument("commands", nargs="+", help="chat commands, one per argument")
    args.add_argument("--pause", type=float, default=0.3, help="seconds between commands (default 0.3)")
    opts = args.parse_args()
    host, _, port = opts.host.partition(":")
    steps = plan(opts.commands, Grid())
    try:
        asyncio.run(drive(host, int(port or 5900), steps, opts.pause))
    except VncError as err:
        sys.exit(f"vnc: {err}")


if __name__ == "__main__":
    main()
