"""Chaos modifiers (PLAN.md 6.11, Phase 9): a minute of mischief, bought with points or
brought by chance.

Every modifier lives in brain or the overlay, never in the target, so each is
undone the moment it ends and none can break a run:

    mirror    Inverted mouse     clicks land mirrored across the screen   injector  60 s
    azerty    AZERTY keyboard    typed text comes out as on an AZERTY     injector  60 s
    dvorak    Dvorak keyboard    ... as on a Dvorak                       injector  60 s
    caps      Caps Lock glued    letters come out in the other case       injector  60 s
    drunk     Drunk mouse        clicks land up to 20 px off              injector  60 s
    lag       Input lag          5 s before each command runs             executor  60 s
    shuffle   Shuffled grid      the grid's labels change places          overlay   60 s
    flip      Upside down        the stream shows the screen upside down  overlay   30 s
    quake     Earthquake         the stream shakes                        overlay   15 s
    blackout  Blackout           the stream shows nothing: chat plays blind overlay 30 s

One at a time, and COOLDOWN seconds between the end of one and the start of
the next. `!buy NAME` spends its price in points (game/points.py); now and
then (RANDOM_EVERY, RANDOM_CHANCE) chance brings one; a Super Chat's tier
picks one (paid: queued, no cooldown, PLAN.md 6.11's "paid triggers buy chaos
only"). Injector modifiers change a command when brain accepts it, so the text
filter sees what will really be typed.
"""

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from chatuses.commands import Click, Drag, InputCommand, MacroRun, Move, StepCommand, TypeText
from chatuses.grid import Grid, ShuffledGrid
from chatuses.store import Store

COOLDOWN = 300.0  # seconds from one modifier's end to the next one's start
RANDOM_EVERY = 1200.0  # seconds between chances of a random modifier
RANDOM_CHANCE = 0.5
LAG = 5.0  # seconds, "lag"
JITTER = 20  # pixels, "drunk"
MAX_QUEUE = 10  # paid modifiers waiting

Source = Literal["points", "random", "superchat", "moderator"]


@dataclass(frozen=True)
class Modifier:
    key: str
    name: str
    what: str
    where: Literal["injector", "executor", "overlay"]
    seconds: int
    cost: int  # points


MODIFIERS: dict[str, Modifier] = {
    m.key: m
    for m in (
        Modifier("mirror", "Inverted mouse", "clicks land mirrored across the screen", "injector", 60, 80),
        Modifier(
            "azerty", "AZERTY keyboard", "typed text comes out as on an AZERTY keyboard", "injector", 60, 80
        ),
        Modifier(
            "dvorak", "Dvorak keyboard", "typed text comes out as on a Dvorak keyboard", "injector", 60, 80
        ),
        Modifier("caps", "Caps Lock glued", "letters come out in the other case", "injector", 60, 40),
        Modifier("drunk", "Drunk mouse", f"clicks land up to {JITTER} px off", "injector", 60, 60),
        Modifier("lag", "Input lag", f"{LAG:.0f} s before each command runs", "executor", 60, 100),
        Modifier("shuffle", "Shuffled grid", "the grid's labels change places", "overlay", 60, 80),
        Modifier("flip", "Upside down", "the stream shows the screen upside down", "overlay", 30, 60),
        Modifier("quake", "Earthquake", "the stream shakes", "overlay", 15, 30),
        Modifier("blackout", "Blackout", "the stream shows nothing: chat plays blind", "overlay", 30, 80),
    )
}
# A Super Chat's tier (1 = the smallest) picks one: chaos only, never a vote.
SUPER_CHAT = {1: "quake", 2: "flip", 3: "caps", 4: "blackout", 5: "shuffle", 6: "mirror"}

_QWERTY = "qwertyuiop[]asdfghjkl;'zxcvbnm,./"
_DVORAK = "',.pyfgcrl/=aoeuidhtns-;qjkxbmwvz"
_AZERTY = {"a": "q", "q": "a", "z": "w", "w": "z", "m": ",", ";": "m", ",": ";"}


def _layout(pairs: dict[str, str]) -> dict[str, str]:
    """Lower and upper case for letters on both sides."""
    table = dict(pairs)
    for key, value in pairs.items():
        if key.isalpha() and value.isalpha():
            table[key.upper()] = value.upper()
    return table


LAYOUTS = {"dvorak": _layout(dict(zip(_QWERTY, _DVORAK, strict=True))), "azerty": _layout(_AZERTY)}


@dataclass
class Active:
    modifier: Modifier
    by: str  # who brought it (as the overlay shows them), or "chance"
    source: Source
    started: float
    until: float
    grid: Grid | None = None  # shuffle: the grid chat aims with meanwhile
    labels: dict[str, list[str]] = field(default_factory=dict[str, list[str]])  # shuffle: for the overlay


class Chaos:
    def __init__(
        self,
        grid: Grid,
        on_change: Callable[["Active | None", "Active | None"], None] = lambda _old, _new: None,
        rng: random.Random | None = None,
    ) -> None:
        self.grid = grid  # the real one
        self.on_change = on_change  # brain: (what ended, what began)
        self.rng = rng or random.Random()
        self.active: Active | None = None
        self.free_at = 0.0  # when the next may start (the cooldown)
        self.next_chance: float | None = None
        self.queue: deque[tuple[Modifier, str]] = deque(maxlen=MAX_QUEUE)  # paid, waiting

    # --- starting one --------------------------------------------------------------------

    def why_not(self, key: str, now: float) -> str | None:
        """Why this one can't start now (None: it can). Fixed wording, for chat."""
        if key not in MODIFIERS:
            return "no such chaos: the site's Laws page lists them"
        if self.active is not None:
            left = max(0.0, self.active.until - now)
            return f"{self.active.modifier.name} is on: {left:.0f} s left"
        if now < self.free_at:
            return f"chaos again in {_wait(self.free_at - now)}"
        return None

    def start(self, key: str, by: str, source: Source, now: float) -> Active:
        """Start it (the caller checked why_not, or it's paid)."""
        modifier = MODIFIERS[key]
        active = Active(modifier, by, source, now, now + modifier.seconds)
        if key == "shuffle":
            cols, rows = list(self.grid.column_labels), list(self.grid.row_labels)
            self.rng.shuffle(cols)
            self.rng.shuffle(rows)
            active.grid = ShuffledGrid(self.grid.width, self.grid.height, tuple(cols), tuple(rows))
            active.labels = {"cols": cols, "rows": rows}
        old, self.active = self.active, active
        self.on_change(old, active)
        return active

    def paid(self, key: str, by: str, now: float) -> bool:
        """A paid trigger (a Super Chat): now if nothing's on, else in turn, cooldown or not.
        False if the queue is full."""
        if self.active is None:
            self.start(key, by, "superchat", now)
            return True
        if len(self.queue) == self.queue.maxlen:
            return False
        self.queue.append((MODIFIERS[key], by))
        return True

    def stop(self, now: float) -> bool:
        """A moderator's call, or a panic: off now (the cooldown applies)."""
        if self.active is None:
            return False
        self._end(now)
        return True

    def tick(self, now: float, chance: bool) -> None:
        """Time passes: one ends, a paid one waiting begins, or chance brings one.
        chance: brain says a random one may come (the feature on, the machine not quiet)."""
        if self.active is not None and now >= self.active.until:
            self._end(now)
        if self.active is None and self.queue:
            modifier, by = self.queue.popleft()
            self.start(modifier.key, by, "superchat", now)
            return
        if self.next_chance is None:
            self.next_chance = now + RANDOM_EVERY
        if now < self.next_chance:
            return
        self.next_chance = now + RANDOM_EVERY
        if chance and self.why_not("quake", now) is None and self.rng.random() < RANDOM_CHANCE:
            self.start(self.rng.choice(sorted(MODIFIERS)), "chance", "random", now)

    def _end(self, now: float) -> None:
        old, self.active = self.active, None
        self.free_at = now + COOLDOWN
        self.on_change(old, None)

    # --- what it does ----------------------------------------------------------------------

    def is_on(self, key: str) -> bool:
        return self.active is not None and self.active.modifier.key == key

    def transform(self, command: InputCommand) -> InputCommand:
        """An injector modifier at work on a command chat sent."""
        if self.active is None or self.active.modifier.where != "injector":
            return command
        if isinstance(command, MacroRun):
            return MacroRun(command.name, tuple(self._step(step) for step in command.steps))
        return self._step(command)

    def _step(self, command: StepCommand) -> StepCommand:
        key = self.active.modifier.key if self.active else ""
        match command:
            case TypeText(text=text) if key in LAYOUTS:
                table = LAYOUTS[key]
                return TypeText("".join(table.get(c, c) for c in text))
            case TypeText(text=text) if key == "caps":
                return TypeText(text.swapcase())
            case Click(x=x, y=y) | Move(x=x, y=y) if key in ("mirror", "drunk"):
                nx, ny = self._point(x, y, key)
                return replace(command, x=nx, y=ny)
            case Drag(start=start, end=end) if key in ("mirror", "drunk"):
                return replace(command, start=self._point(*start, key), end=self._point(*end, key))
            case _:
                return command

    def _point(self, x: int, y: int, key: str) -> tuple[int, int]:
        w, h = self.grid.width, self.grid.height
        if key == "mirror":
            return w - 1 - x, h - 1 - y
        jx, jy = self.rng.randint(-JITTER, JITTER), self.rng.randint(-JITTER, JITTER)
        return min(w - 1, max(0, x + jx)), min(h - 1, max(0, y + jy))

    def delay(self) -> float:
        """Seconds the executor waits before a command (lag)."""
        return LAG if self.is_on("lag") else 0.0

    def aim_grid(self) -> Grid:
        """The grid chat aims with now: shuffled while "shuffle" is on."""
        active = self.active
        return active.grid if active is not None and active.grid is not None else self.grid

    def view(self, now: float) -> dict[str, Any] | None:
        """For the overlay: what, who, how long, and the shuffled labels."""
        active = self.active
        if active is None:
            return None
        view: dict[str, Any] = {
            "key": active.modifier.key,
            "name": active.modifier.name,
            "by": active.by,
            "source": active.source,
            "ends_in": round(max(0.0, active.until - now), 1),
        }
        if active.labels:
            view["grid"] = active.labels
        return view


def buy(chaos: Chaos, store: Store, user: str, name: str, key: str, now: float) -> tuple[bool, str]:
    """!buy NAME: a modifier for its price in points, if it can start. (ok, words for chat)"""
    if why := chaos.why_not(key, now):
        return False, why
    modifier = MODIFIERS[key]
    if not store.spend_points(user, modifier.cost):
        have = store.standing(user)[0]
        return False, f"{modifier.name} costs {modifier.cost} points: you have {have}"
    store.event(now, "chaos", {"user": user, "key": key, "source": "points", "cost": modifier.cost})
    chaos.start(key, name, "points", now)
    return True, f"{modifier.name} for {modifier.seconds} s"


def _wait(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"
