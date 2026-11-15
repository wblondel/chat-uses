# chat-uses control plane ("brain")

Turns chat messages into input for the target desktop (PLAN.md 5.7, Phase 4).

```
chat message -> moderation gate -> parser -> rate limits -> text filter -> accepted
             -> executor (one at a time) -> VNC injector -> target desktop
```

So far: the parser for chat's commands (`parser.py`, PLAN.md 6.1), the click
grid (`grid.py`, 6.2), the key map for VNC (`keymap.py`), rate limits
(`ratelimit.py`), identity and presence (`identity.py`, 6.5), the SQLite store
with its append-only event log (`store.py`, 5.8), the pipeline tying them
together (`pipeline.py`), and a chat simulator (`sim/`).

Then the output side: `vnc.py`, an input-only VNC client that treats the
server as hostile (PLAN.md 5.4), and `executor.py`, which runs accepted
commands on it one at a time, refuses new ones when 16 are waiting, drops the
waiting ones if the target goes away, and reconnects on its own. `drive.py`
sends commands from the command line, for trying a desktop by hand; on the
server, see `docs/phase4.md`.

## Develop

```bash
cd control
uv sync                       # Python 3.13 and the dev tools
uv run pytest                 # tests
uv run ruff check && uv run ruff format --check && uv run pyright
uv run python -m chatuses.sim --users 50 --seconds 60   # simulated chat through the pipeline
```

The VNC tests run against a fake server (`tests/fake_vnc.py`): well-behaved,
it records every key and pointer event it receives; hostile, it sends
whatever bytes a test scripts.

To drive a real desktop (from a container that may reach its VNC port, not
from the host):

```bash
PYTHONPATH=src python3 -m chatuses.drive 10.30.0.251 '!key ctrl+alt+t' '!wait 3' '!type echo hi' '!enter'
```

The runtime uses only Python's standard library so far.
