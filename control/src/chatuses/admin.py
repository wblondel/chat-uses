"""chat-brain-admin: what brain takes from its operator, over the console socket.

    chat-brain-admin status                     brain, the desktop link, the chat adapters
    chat-brain-admin youtube VIDEO_ID_OR_URL    read this live stream's chat (kept across restarts)
    chat-brain-admin youtube off                stop reading YouTube chat

Moderation (PLAN.md 7.5, 7.6), as Discord's slash commands and the admin
dashboard do it; logged as done by "console":

    chat-brain-admin panic [REASON]             cover the stream, mute the target, freeze input
    chat-brain-admin resume                     end the panic
    chat-brain-admin ban WHO DURATION [REASON]  WHO: the name the overlay showed, or yt:... / dc:...
                                                DURATION: 30m, 2h, 7d, 1w, perm
    chat-brain-admin timeout WHO [MINUTES]      10 minutes unless told
    chat-brain-admin unban WHO
    chat-brain-admin lockdown on|off            only regulars may send commands
    chat-brain-admin bans                       who is banned or timed out, until when
    chat-brain-admin endvote                    end the power vote running now
    chat-brain-admin mode anarchy|democracy     set the mode now (locked for a while after)
    chat-brain-admin snapshots                  chat's snapshots: number, kind, label
    chat-brain-admin snapshot                   take an auto snapshot now
    chat-brain-admin rewind-to NUMBER|SAVE      put the target back to one, no vote

Rulesets and laws (PLAN.md 6.12, rulesets/):

    chat-brain-admin ruleset [NAME]             the ruleset in use (and those there are), or switch
    chat-brain-admin laws                       laws changed over the ruleset's
    chat-brain-admin law NAME VALUE|default     change a law (within its bounds), or put it back

Screen OCR verdicts (Phase 7): was each hit real? What decides enforcing.

    chat-brain-admin ocr                        the hits of the last 90 days and their verdicts
    chat-brain-admin ocr-verdict HIT real|false a hit's verdict (also: the buttons in #mod-alerts)

Sessions (PLAN.md Phase 7): infra/host/session.py runs these and adds the
encoder and CPU; the report is posted in #ops and kept (counts only).

    chat-brain-admin session                    the session running, if any
    chat-brain-admin session start NAME         brain notes viewers and chat every minute
    chat-brain-admin session end                the report: viewers, chat, commands, votes, panics, holds
    chat-brain-admin sessions                   the last 10 sessions

Privacy (PLAN.md 11, docs/privacy.md): commands and logs are deleted after 90 days.

    chat-brain-admin forget WHO                 a deletion request: their commands and log entries

Runs inside the brain container (root): python -m chatuses.admin.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from chatuses.console import request


def main() -> None:
    args = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        epilog=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    args.add_argument("--socket", default=os.environ.get("BRAIN_CONSOLE", "/run/chat-brain/console.sock"))
    args.add_argument("words", nargs="+", help="what to do (below)")
    opts = args.parse_args()
    try:
        reply = asyncio.run(request(Path(opts.socket), {"admin": opts.words}))
    except (OSError, TimeoutError, ValueError) as err:
        sys.exit(f"can't reach brain at {opts.socket}: {err or type(err).__name__}")
    print(reply.get("reason") or reply.get("outcome"))
    sys.exit(0 if reply.get("outcome") == "ok" else 1)


if __name__ == "__main__":
    main()
