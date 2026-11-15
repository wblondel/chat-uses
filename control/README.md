# chat-uses control plane ("brain")

Turns chat messages into input for the target desktop (PLAN.md 5.7, Phase 4).

```
chat message -> moderation gate -> parser -> rate limits -> text filter -> accepted
```

So far: the parser for chat's commands (`parser.py`, PLAN.md 6.1), the click
grid (`grid.py`, 6.2), the key map for VNC (`keymap.py`), rate limits
(`ratelimit.py`), identity and presence (`identity.py`, 6.5), the SQLite store
with its append-only event log (`store.py`, 5.8), the pipeline tying them
together (`pipeline.py`), and a chat simulator (`sim/`).

## Develop

```bash
cd control
uv sync                       # Python 3.13 and the dev tools
uv run pytest                 # tests
uv run ruff check && uv run ruff format --check && uv run pyright
uv run python -m chatuses.sim --users 50 --seconds 60   # simulated chat through the pipeline
```

The runtime uses only Python's standard library so far.
