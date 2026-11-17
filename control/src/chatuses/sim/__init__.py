"""Chat simulator (PLAN.md 5.7, sim/): fake users through the real pipeline.

On a simulated clock, so a minute of chat runs in milliseconds. Behaviours:

- regular: a valid command every 3-15 s;
- spammer: a valid command every 0.2-0.5 s;
- troll: malformed and hostile messages every 1-5 s;
- chatter: ordinary chat every 5-20 s.

It reports outcomes, the busiest second of accepted input (the global rate
limit must hold) and the processing time per message.
"""

import heapq
import random
import time
from collections import Counter
from dataclasses import dataclass, field

from chatuses.commands import InputCommand
from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.store import Store

VALID = [
    "!type hello world",
    "!enter",
    "!click F7.5",
    "!dclick C2",
    "!rclick K4",
    '!click "Settings"',
    "!dclick “Trash”#2",
    "!key ctrl+alt+t",
    "!key alt+f4",
    "!scroll down 3",
    "!drag B2 H6",
    "!bksp 3",
    "!move K3.9",
    "!up 2",
    "!anarchy",
    "!democracy",
    "!help",
]
HOSTILE = [
    "!type " + "a" * 500,
    "!click Z99",
    "!click F0",
    "!key ctrl+ctrl+x",
    "!key " + " ".join(["ctrl+a"] * 9),
    "!type \u202eevil",
    "!type a\nb",
    "!ty\u200bpe hidden",
    "!" + "x" * 300,
    "\uff01\uff54\uff59\uff50\uff45 \uff48\uff49",  # fullwidth !type hi: valid after NFKC
    "!scroll sideways",
    "!bksp 99999999999999999999",
    "!type \U0001f600",
    "!macro x = !type y",
    "!rewind -1",
    "!save ../../etc/passwd",
    "!drag A1",
    '!click "' + "x" * 300 + '"',
    '!click "unclosed',
    '!click "a"#0',
    '!click "\u202eevil"',
    '!macro mm = click "Settings"',
    "!type",
    "!\x00type nul",
]
CHAT = ["lol", "gg", "what is this", "!!!", "go left!", "who's driving?", "ok"]

BEHAVIOURS: dict[str, tuple[float, float, list[str]]] = {
    "regular": (3.0, 15.0, VALID),
    "spammer": (0.2, 0.5, VALID),
    "troll": (1.0, 5.0, HOSTILE),
    "chatter": (5.0, 20.0, CHAT),
}
DEFAULT_MIX = {"regular": 0.6, "spammer": 0.15, "troll": 0.15, "chatter": 0.1}


@dataclass
class Report:
    messages: int = 0
    outcomes: Counter[str] = field(default_factory=Counter[str])
    busiest_second: int = 0  # most input commands accepted in one second
    accepted_inputs: int = 0
    seconds: float = 0.0
    timings: list[float] = field(default_factory=list[float])  # seconds per message

    def percentile(self, p: float) -> float:
        if not self.timings:
            return 0.0
        ordered = sorted(self.timings)
        return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]


def simulate(
    users: int, seconds: float, seed: int = 1, mix: dict[str, float] | None = None, store: Store | None = None
) -> Report:
    if store is None:
        with Store(":memory:") as own:
            return simulate(users, seconds, seed, mix, own)
    rng = random.Random(seed)
    pipeline = Pipeline(store)
    mix = mix or DEFAULT_MIX
    kinds = rng.choices(list(mix), weights=list(mix.values()), k=users)
    queue: list[tuple[float, int]] = [(rng.uniform(0, BEHAVIOURS[k][1]), i) for i, k in enumerate(kinds)]
    heapq.heapify(queue)
    report = Report(seconds=seconds)
    per_second: Counter[int] = Counter()
    while queue:
        now, i = heapq.heappop(queue)
        if now > seconds:
            break
        low, high, lines = BEHAVIOURS[kinds[i]]
        msg = ChatMessage(f"yt:sim{i}", f"sim{i}", rng.choice(lines), now)
        start = time.perf_counter()
        outcome = pipeline.handle(msg)
        report.timings.append(time.perf_counter() - start)
        report.messages += 1
        report.outcomes[outcome.kind] += 1
        if outcome.kind == "accepted" and isinstance(outcome.command, InputCommand):
            per_second[int(now)] += 1
            report.accepted_inputs += 1
        heapq.heappush(queue, (now + rng.uniform(low, high), i))
    report.busiest_second = max(per_second.values(), default=0)
    return report
