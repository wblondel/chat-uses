"""chat-brain-admin: what brain takes from its operator, over the console socket.

    chat-brain-admin status                     brain, the desktop link, the chat adapters
    chat-brain-admin youtube VIDEO_ID_OR_URL    read this live stream's chat (kept across restarts)
    chat-brain-admin youtube off                stop reading YouTube chat

Runs inside the brain container (root): python -m chatuses.admin. Phase 5's
admin dashboard does the same over an authenticated API.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from chatuses.console import request


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    args.add_argument("--socket", default=os.environ.get("BRAIN_CONSOLE", "/run/chat-brain/console.sock"))
    args.add_argument("words", nargs="+", help="status | youtube VIDEO_ID_OR_URL | youtube off")
    opts = args.parse_args()
    try:
        reply = asyncio.run(request(Path(opts.socket), {"admin": opts.words}))
    except (OSError, TimeoutError, ValueError) as err:
        sys.exit(f"can't reach brain at {opts.socket}: {err or type(err).__name__}")
    print(reply.get("reason") or reply.get("outcome"))
    sys.exit(0 if reply.get("outcome") == "ok" else 1)


if __name__ == "__main__":
    main()
