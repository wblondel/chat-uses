"""Governance scenarios (PLAN.md Phase 6): crowds voting through the real
pipeline, referee and lockdown, on a simulated clock.

- **fifty:** 50 people play while a `!rewind` vote runs; it passes the
  veto countdown and runs once.
- **split:** a 60/40 split fails a vote that needs 66%; the same action
  can't be proposed again during its cooldown, another one can, and a 52%
  majority passes a vote that needs half.
- **brigade:** 60 new accounts raid a vote: their ballots, pushes and
  proposals are refused, and they raise the quorum (D54) rather than swing
  anything. Two minutes later they could vote; lockdown keeps them out.
- **veto:** a passed `!rewind` is stopped by a third of its yes count in
  vetoes, from people who were around.
- **low population:** 3 people: the quorum's floor stops 1 or 2 people
  deciding alone, and 2 of 3 pass a reboot.
- **ties:** democracy's tie goes to the command voted first, a changed
  ballot counts once, and a power vote split exactly in half passes a vote
  that needs 50% (the threshold is "at least").
- **macros:** chat votes a macro in (Phase 9, PLAN.md 6.10); anyone can run
  it, as one command that rate-limits like one; a macro can't hold macros,
  take a command's name or hold more than 8 commands; a slur in one is
  refused before the vote; a vote takes it out again.
- **driver:** after 10 minutes of play the driver's seat is offered to
  someone who played (not to someone with 2 commands); their command takes
  it, everyone else's input is refused for the drive while votes still
  work; `!impeach` ends it; the next offer goes to someone else, and an
  offer nobody takes runs out.
- **chaos:** too few points can't buy a modifier, enough can; one at a
  time, five minutes apart; an inverted mouse mirrors a click, a shuffled
  grid sends chat's click where its label now is; Super Chats queue and run
  one after another, cooldown or not.
- **amendments:** 60 % can't amend a law (it takes 75 %); 12 of 30 who
  agree can, through the veto countdown, and the law changes; another
  amendment waits an hour; what the constitution doesn't allow (out of
  bounds, `!newrun`'s rules, an unknown law) is refused before any vote.
- **night:** 15 minutes after the last moderator spoke, night falls
  (PLAN.md 7.6, safety/night.py): democracy, `!anarchy` refused, a
  `!rewind` that 5 of 12 would pass by day fails for want of 6, commands
  come one every 4 seconds; a moderator's message brings the day back, and
  anarchy with it. (Every other scenario runs with night mode off.)

Every scenario is seeded and checks what happened; `python -m chatuses.sim
--scenarios` runs them all (CI does) and fails if one check does.
"""

import asyncio
import heapq
import itertools
import math
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from chatuses.commands import (
    Amend,
    Buy,
    Click,
    InputCommand,
    Load,
    MacroDefine,
    MacroRemove,
    Meta,
    Rewind,
    Save,
    StepCommand,
)
from chatuses.game import chaos as chaos_module
from chatuses.game.chaos import COOLDOWN, MODIFIERS, Chaos
from chatuses.game.driver import DriverSeat
from chatuses.pipeline import ChatMessage, Pipeline
from chatuses.referee.constitution import UNKNOWN
from chatuses.referee.modes import Winner
from chatuses.referee.referee import NIGHT_MODE, Referee
from chatuses.referee.votes import NOT_ELIGIBLE, Proposal, Vote
from chatuses.safety.moderation import MODERATOR_ROLES, Moderation
from chatuses.safety.night import Change, Night
from chatuses.safety.strikes import Strikes
from chatuses.safety.textfilter import HATE, LISTS
from chatuses.store import Store

T0 = 1_000_000.0  # the simulated clock's start (seconds)
TICK = 0.25  # as brain's referee loop
OVERLAY_ONLY = ("grid", "help", "rules")  # as brain: the overlay answers these
PLAY = ["!click F7", "!type hello", "!enter", "!scroll down 3", "!key alt+tab", "!move K3"]


@dataclass(frozen=True)
class Said:
    at: float
    user: str
    text: str
    reply: str  # "ok", or why not


@dataclass(frozen=True)
class Check:
    what: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    name: str
    checks: list[Check] = field(default_factory=list[Check])

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def check(self, what: str, ok: bool, detail: object = "") -> None:
        self.checks.append(Check(what, ok, str(detail)))


class _Quiet:
    """What moderation does to the stream: nothing, here."""

    def panic_changed(self, on: bool, kind: str = "panic") -> None: ...
    def lockdown_changed(self, on: bool) -> None: ...
    def alert(self, text: str) -> None: ...
    def snapshot(self, name: str) -> None: ...
    def end_vote(self, why: str) -> tuple[bool, str]:
        return False, ""

    def rewind_to(self, which: str, by: str) -> tuple[bool, str]:
        return False, ""

    def set_mode(self, mode: str, by: str) -> tuple[bool, str]:
        return False, ""

    def moderator_here(self, by: str, seconds: float) -> None: ...
    def set_night(self, setting: str, by: str) -> tuple[bool, str]:
        return False, ""


class Room:
    """Chat around the real pipeline and referee, routed as brain routes it."""

    def __init__(self, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.now = T0
        self.store = Store(":memory:")
        self.moderation = Moderation(self.store, _Quiet(), Strikes())
        self.macros: dict[str, tuple[StepCommand, ...]] = {}
        self.pipeline = Pipeline(
            self.store, locked_out=self.moderation.held_by_lockdown, macros=lambda: self.macros
        )
        self.won: list[Winner] = []  # democracy's winners
        self.referee = Referee(
            self.store, self.pipeline.presence, self, clock=lambda: self.now, run_winner=self.won.append
        )
        self.driven: list[tuple[str, str]] = []  # the driver's seat: (event, user)
        self.driver = DriverSeat(
            lambda: self.referee.laws,
            self.pipeline.presence,
            lambda _now: self.store.command_counts(T0 - 1),
            lambda event, seat: self.driven.append((event, seat.user)),
            self.rng,
        )
        self.ran_inputs: list[InputCommand] = []  # what reached the machine, chaos and all
        self.chaos = Chaos(
            self.pipeline.grid,
            lambda _old, _new: setattr(self.pipeline, "grid", self.chaos.aim_grid()),
            self.rng,
        )
        self.roles: dict[str, frozenset[str]] = {}
        self.ran: list[str] = []  # what passed votes did
        self.said: list[Said] = []
        self.inputs = 0  # input commands that ran (anarchy) or were counted (democracy)
        self.votes: list[Vote] = []  # every vote opened, as it ended
        self.night = Night(self.store, self.now)
        self.night.set("off", "the simulator")  # the night scenario sets it auto
        self.nights: list[Change] = []  # night falling, day coming back
        self._events: list[tuple[float, int, str, str]] = []
        self._order = itertools.count()

    def __enter__(self) -> "Room":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.store.close()

    # Brain's side of the referee (referee.Actions): everything is available, and works.

    def unavailable(self, proposal: Proposal) -> str | None:
        if proposal.action == "impeach" and self.driver.seat is None:
            return "nobody is driving"
        return None

    async def run(self, proposal: Proposal) -> tuple[bool, str]:
        self.ran.append(proposal.text)
        if proposal.action == "macro":
            self.macros[str(proposal.arg)] = proposal.steps
        elif proposal.action == "unmacro":
            self.macros.pop(str(proposal.arg), None)
        elif proposal.action == "impeach":
            self.driver.end(self.now, "impeached")
        elif proposal.action == "amend":
            vote = self.referee.votes.current
            return True, self.referee.amend(
                str(proposal.arg), proposal.value or 0, vote.proposer if vote else "", None
            )[1]
        return True, "done"

    # --- people ----------------------------------------------------------------------

    def arrive(self, users: Iterable[str], ago: float, roles: Iterable[str] = ()) -> list[str]:
        """People first seen `ago` seconds back, who spoke a moment ago."""
        users = list(users)
        for user in users:
            self.pipeline.presence.saw(user, self.now - ago)
            self.pipeline.presence.saw(user, self.now - 5)
            self.roles[user] = frozenset(roles)
        return users

    def settled(self, n: int, prefix: str = "u") -> list[str]:
        """n people around for 10 minutes, regulars."""
        return self.arrive((f"{prefix}{i}" for i in range(n)), 600, {"regular"})

    def say(self, user: str, text: str) -> str:
        msg = ChatMessage(user, user, text, self.now, self.roles.get(user, frozenset()))
        if msg.roles & MODERATOR_ROLES:  # as brain: a moderator is around
            self.night.saw(user, self.now)
            self.night_tick()
        outcome = self.pipeline.handle(msg)
        command = outcome.command
        reply = outcome.reason or outcome.kind
        if outcome.kind == "accepted":
            reply = "ok"
            if isinstance(command, InputCommand):
                if self.referee.democracy:
                    self.referee.ballot(user, user, command, self.now)
                    self.inputs += 1
                elif why := self.driver.command(user, self.now):
                    reply = why
                else:
                    self.inputs += 1
                    self.ran_inputs.append(self.chaos.transform(command))
            elif isinstance(command, Buy):
                _ok, reply = chaos_module.buy(self.chaos, self.store, user, user, command.key, self.now)
                reply = "ok" if _ok else reply
            elif isinstance(command, (Rewind, Load, Save, MacroDefine, MacroRemove, Amend)) or (
                isinstance(command, Meta) and command.name not in OVERLAY_ONLY
            ):
                answer = self.referee.handle(user, user, command, self.now)
                reply = "ok" if answer.ok else answer.text
                vote = self.referee.votes.current
                if vote is not None and all(v is not vote for v in self.votes):
                    self.votes.append(vote)
        self.said.append(Said(self.now, user, text, reply))
        return reply

    def night_tick(self) -> None:
        """As brain's: night falls on the referee and the rate limits, or lifts."""
        if (change := self.night.tick(self.now)) is not None:
            self.referee.set_night(change.on)
            self.pipeline.limiter.set_night(change.on)
            self.nights.append(change)

    def later(self, delay: float, user: str, text: str) -> None:
        heapq.heappush(self._events, (self.now + delay, next(self._order), user, text))

    def each(self, users: Iterable[str], text: str, within: tuple[float, float]) -> None:
        """Each of them says it once, at a random time in the next `within` seconds."""
        for user in users:
            self.later(self.rng.uniform(*within), user, text)

    def play(self, users: Iterable[str], seconds: float, every: tuple[float, float] = (2.0, 8.0)) -> None:
        """They play (input commands) for the next `seconds`."""
        for user in users:
            t = self.rng.uniform(0, every[1])
            while t < seconds:
                self.later(t, user, self.rng.choice(PLAY))
                t += self.rng.uniform(*every)

    async def wait(self, seconds: float) -> None:
        """Time passes: what was said is said, the referee ticks, passed votes run."""
        end = self.now + seconds
        tick = self.now + TICK
        while True:
            event = self._events[0][0] if self._events else math.inf
            if min(event, tick) > end:
                break
            if event <= tick:
                self.now, _, user, text = heapq.heappop(self._events)
                self.say(user, text)
            else:
                self.now = tick
                self.night_tick()
                self.referee.tick()
                self.driver.tick(self.now, not self.referee.democracy and not self.night.on, lambda u: u)
                self.chaos.tick(self.now, chance=False)  # chance's own timing: tests/test_chaos.py
                tick += TICK
                await asyncio.sleep(0)  # a passed vote's action runs
        self.now = end

    async def burst(self, user: str, text: str, times: int) -> list[str]:
        """They say it `times` times, 0.3 s apart: what each got."""
        replies: list[str] = []
        for _ in range(times):
            replies.append(self.say(user, text))
            await self.wait(0.3)
        return replies

    def replies(self, text: str, users: Iterable[str] | None = None) -> Counter[str]:
        """How what they said was answered."""
        who = set(users) if users is not None else None
        return Counter(s.reply for s in self.said if s.text == text and (who is None or s.user in who))


# --- scenarios -------------------------------------------------------------------------


async def fifty(room: Room, result: Result) -> None:
    people = room.settled(50)
    room.play(people, 70)
    await room.wait(5)
    result.check("u0 proposes !rewind", room.say("u0", "!rewind") == "ok")
    vote = room.votes[0]
    result.check("the quorum is a quarter of the 50 active", vote.quorum == 13, vote.quorum)
    room.each(people[1:36], "!yes", (1, 25))
    room.each(people[36:46], "!no", (1, 25))
    await room.wait(30)
    result.check("36 yes and 10 no: it passes (78%, 66% needed)", vote.phase == "veto", (vote.yes, vote.no))
    room.each(people[40:42], "!veto", (1, 10))
    await room.wait(31)
    needed = room.referee.votes.veto_needed(vote)
    result.check(f"2 vetoes of {needed} needed don't stop it", vote.phase == "done", vote.phase)
    result.check("it ran once", room.ran == ["!rewind"], room.ran)
    result.check("chat kept playing during the vote", room.inputs > 300, room.inputs)


async def split(room: Room, result: Result) -> None:
    people = room.settled(50)
    room.say("u0", "!rewind")
    room.each(people[1:30], "!yes", (1, 25))
    room.each(people[30:50], "!no", (1, 25))
    await room.wait(31)
    vote = room.votes[0]
    result.check("30 yes, 20 no: !rewind fails (60%, 66% needed)", vote.phase == "failed", vote.result)
    result.check("nothing ran", room.ran == [], room.ran)
    again = room.say("u1", "!rewind")
    result.check(
        "!rewind can't be proposed again at once", again.startswith("!rewind was just voted on"), again
    )
    result.check("another action can", room.say("u1", "!reboot") == "ok")
    room.each(people[2:27], "!yes", (1, 25))  # with u1: 26 yes
    room.each([people[0], *people[27:]], "!no", (1, 25))
    await room.wait(31)
    reboot = room.votes[1]
    result.check("26 yes, 24 no: !reboot passes (52%, half needed)", reboot.phase == "done", reboot.result)
    result.check("it ran", room.ran == ["!reboot"], room.ran)


async def brigade(room: Room, result: Result) -> None:
    people = room.settled(20)
    raiders = room.arrive((f"raid{i}" for i in range(60)), 30)  # just arrived
    room.play(raiders, 250, (1.0, 3.0))  # they spam, so they stay active
    room.play(people, 250)
    await room.wait(1)
    result.check("u0 proposes !reboot", room.say("u0", "!reboot") == "ok")
    vote = room.votes[0]
    result.check("the raid raises the quorum (D54): 15% of 80 active", vote.quorum == 12, vote.quorum)
    room.each(raiders, "!yes", (0.5, 20))
    room.each(raiders, "!democracy", (0.5, 20))
    room.each(people[1:8], "!yes", (1, 25))
    room.each(people[8:20], "!no", (1, 25))
    await room.wait(31)
    yes = room.replies("!yes", raiders)
    result.check("every raider's ballot is refused", yes == Counter({NOT_ELIGIBLE: 60}), dict(yes))
    result.check("8 yes, 12 no: it fails", vote.phase == "failed" and vote.yes == 8, (vote.yes, vote.no))
    pushes = room.replies("!democracy", raiders)
    result.check(
        "no raider moves the meter", pushes["ok"] == 0 and room.referee.modes.meter == 50, dict(pushes)
    )
    proposal = room.say("raid0", "!reset")
    result.check("a raider can't propose", proposal.startswith("you can propose votes once"), proposal)
    await room.wait(180)  # the raid waits it out: now they could vote
    room.moderation.lockdown("a moderator", True)
    result.check("under lockdown, u0 proposes !reboot again", room.say("u0", "!reboot") == "ok")
    again = room.votes[1]
    result.check("lockdown doesn't lower the quorum: they're still active", again.quorum == 12, again.quorum)
    room.each(raiders, "!yes", (0.5, 20))
    room.each(people[1:12], "!yes", (1, 25))
    room.each(people[12:20], "!no", (1, 25))
    await room.wait(31)
    yes = room.replies("!yes", raiders)
    locked = Counter(r for r in yes.elements() if r != NOT_ELIGIBLE)
    result.check(
        "lockdown keeps the waiting raiders out", "ok" not in yes and sum(locked.values()) == 60, dict(yes)
    )
    result.check("the regulars decide: 12 yes, 8 no, it passes", again.phase == "done", (again.yes, again.no))


async def veto(room: Room, result: Result) -> None:
    people = room.settled(30)
    late = room.arrive(["late0"], 30)
    room.say("u0", "!rewind")
    room.each(people[1:20], "!yes", (1, 25))
    room.each(people[20:25], "!no", (1, 25))
    await room.wait(31)
    vote = room.votes[0]
    needed = room.referee.votes.veto_needed(vote)
    result.check("20 yes, 5 no: it passes to the countdown", vote.phase == "veto", (vote.yes, vote.no))
    result.check("a third of the yes count is needed to veto", needed == math.ceil(20 / 3), needed)
    result.check("someone who just arrived can't veto", room.say(late[0], "!veto") == NOT_ELIGIBLE)
    room.each(people[-needed:], "!veto", (1, 20))
    await room.wait(21)
    result.check(f"{needed} vetoes stop it", vote.phase == "vetoed", vote.phase)
    result.check("nothing ran", room.ran == [], room.ran)


async def low_population(room: Room, result: Result) -> None:
    room.settled(3)
    room.say("u0", "!save base")
    await room.wait(31)
    save = room.votes[0]
    result.check("1 of 3 can't save alone (quorum floor 2)", save.phase == "failed", save.result)
    room.say("u0", "!rewind")
    room.later(2, "u1", "!yes")
    await room.wait(31)
    rewind = room.votes[1]
    result.check("2 of 3 can't rewind (floor 3)", rewind.phase == "failed", rewind.result)
    room.say("u0", "!reboot")
    room.later(2, "u1", "!yes")
    room.later(3, "u2", "!no")
    await room.wait(31)
    reboot = room.votes[2]
    result.check("2 of 3 reboot (floor 2, 67%)", reboot.phase == "done", reboot.result)
    result.check("only the reboot ran", room.ran == ["!reboot"], room.ran)


async def ties(room: Room, result: Result) -> None:
    people = room.settled(20)
    for user in people[:15]:  # 15 pushes of 2: 50 to 80
        room.say(user, "!democracy")
    result.check("15 pushes make it democracy", room.referee.democracy, room.referee.modes.meter)
    room.later(1, "u0", "!click A1")
    for i, user in enumerate(people[1:5]):
        room.later(2 + i, user, "!click A1")
    for i, user in enumerate(people[5:10]):
        room.later(1.5 + i, user, "!click F7")
    await room.wait(14)
    first = room.won[0] if room.won else None
    tie = (first.text, first.votes, first.total) if first else None
    result.check("5 to 5: the command voted first wins", tie == ("!click A1", 5, 10), tie)
    for i, user in enumerate(people[:5]):
        room.later(1 + i, user, "!click A1")
    for i, user in enumerate(people[5:10]):
        room.later(1.5 + i, user, "!click F7")
    room.later(8, "u0", "!click F7")  # changed their mind
    await room.wait(14)
    second = room.won[1] if len(room.won) > 1 else None
    changed = (second.text, second.votes, second.total) if second else None
    result.check("a changed ballot counts once: 6 to 4", changed == ("!click F7", 6, 10), changed)
    room.referee.set_mode("anarchy", "a moderator")
    room.say("u10", "!reboot")
    room.each(people[11:20], "!yes", (1, 25))  # with u10: 10 yes
    room.each(people[:10], "!no", (1, 25))
    await room.wait(31)
    vote = room.votes[0]
    result.check(
        "10 to 10 on !reboot (half needed) passes", vote.phase == "done", (vote.yes, vote.no, vote.result)
    )


async def macros(room: Room, result: Result) -> None:
    people = room.settled(20)
    room.say("u0", "!macro hello = type hello world; enter")
    room.each(people[1:8], "!yes", (1, 50))
    await room.wait(61)  # a minute to vote
    vote = room.votes[0]
    result.check(
        "8 of 20 vote a macro in (the proposer and 7)",
        vote.phase == "done" and "hello" in room.macros,
        vote.result,
    )
    inputs = room.inputs
    result.check("anyone runs it: !hello", room.say("u12", "!hello") == "ok" and room.inputs == inputs + 1)
    result.check(
        "it counts as one command for the rate limit",
        [room.say("u13", "!hello") for _ in range(4)].count("ok") == 3,
        room.replies("!hello", ["u13"]),
    )
    refused = {
        "!macro again = hello": "a macro inside a macro",
        "!macro click = enter": "a built-in's name",
        "!macro long = " + "; ".join(["enter"] * 9): "nine commands",
        "!macro ab = rewind": "a vote inside a macro",
    }
    for i, (text, what) in enumerate(refused.items()):
        result.check(f"refused: {what}", room.say(f"u{14 + i}", text) not in ("ok", "accepted"), text)
    word = _a_listed_word()  # from the filter's own lists: no slur is written here
    reply = room.say("u18", f"!macro ab = type {word}")
    result.check("a slur in a macro is refused before any vote", reply == HATE and len(room.votes) == 1)
    room.say("u1", "!unmacro hello")
    room.each(people[2:9], "!yes", (1, 50))
    await room.wait(61)
    result.check("a vote takes it out again", "hello" not in room.macros, room.votes[-1].result)
    result.check("!hello is unknown again", room.say("u12", "!hello").startswith("unknown command"))


async def driver(room: Room, result: Result) -> None:
    people = room.settled(20)
    newcomer = room.arrive(["new0"], 600)[0]
    room.play(people, 590)
    room.later(30, newcomer, "!click A1")
    room.later(60, newcomer, "!enter")
    room.later(580, newcomer, "!hi")  # chat: still around, with 2 commands
    await room.wait(601)
    seat = room.driver.seat
    result.check(
        "after 10 minutes the seat is offered to someone who played",
        seat is not None and seat.user in people,
        seat,
    )
    result.check("never to someone with 2 commands", newcomer not in room.driver.candidates(room.now))
    assert seat is not None
    driver = seat.user
    others = [p for p in people if p != driver]
    result.check("others still play while it's only offered", room.say(others[0], "!click F7") == "ok")
    result.check("the driver's command takes the wheel and runs", room.say(driver, "!enter") == "ok")
    result.check("a 60 s drive", seat.state == "driving" and seat.until - room.now == 60)
    refused = room.say(others[1], "!click F7")
    result.check("everyone else's input is refused", refused.startswith(f"{driver} is driving"), refused)
    result.check("votes still work: the meter", room.say(others[2], "!democracy") == "ok")
    result.check("the driver keeps driving", room.say(driver, "!type hi") == "ok")
    room.say(others[3], "!impeach")
    room.each(others[4:8], "!yes", (1, 15))
    await room.wait(21)
    result.check(
        "!impeach ends the drive",
        room.driver.seat is None and room.driven[-1] == ("impeached", driver),
        room.driven[-1:],
    )
    result.check("everyone plays again", room.say(others[1], "!click F7") == "ok")
    room.play(people, 590)  # play stops just before the next offer: nobody answers it
    await room.wait(601)
    second = room.driver.seat
    result.check("the next offer goes to someone else", second is not None and second.user != driver, second)
    await room.wait(16)
    result.check(
        "an offer nobody takes runs out",
        room.driver.seat is None and room.driven[-1][0] == "declined",
        room.driven[-1:],
    )


async def chaos(room: Room, result: Result) -> None:
    people = room.settled(10)

    def give(user: str, points: int) -> None:
        room.store.seen_user(user, user, room.now, frozenset())
        room.store.add_points([user], points)

    give("u0", 100)
    give("u1", 10)
    reply = room.say("u1", "!buy quake")
    result.check(
        "10 points can't buy an earthquake (30)", reply == "Earthquake costs 30 points: you have 10", reply
    )
    result.check("100 points buy an inverted mouse (80)", room.say("u0", "!buy mirror") == "ok")
    result.check("the points are spent", room.store.standing("u0")[0] == 20)
    reply = room.say("u2", "!buy quake")
    result.check("one at a time", reply.startswith("Inverted mouse is on:"), reply)
    room.say("u3", "!click A1")
    click = room.ran_inputs[-1]
    result.check(
        "a click on A1 lands mirrored", isinstance(click, Click) and (click.x, click.y) == (1549, 849), click
    )
    await room.wait(61)
    give("u0", 100)
    reply = room.say("u0", "!buy quake")
    result.check("five minutes before the next", reply.startswith("chaos again in"), reply)
    room.now += COOLDOWN
    room.settled(10)
    give("u4", 100)
    result.check("then a shuffled grid", room.say("u4", "!buy shuffle") == "ok")
    labels = room.chaos.active.labels if room.chaos.active else {}
    shown = labels["cols"][0] + labels["rows"][0]  # the label over the real A1
    room.say("u5", f"!click {shown}")
    click = room.ran_inputs[-1]
    result.check(
        f"chat clicks {shown}, where A1 now shows that label",
        isinstance(click, Click) and (click.x, click.y) == (50, 50),
        click,
    )
    await room.wait(61)
    result.check(
        "the grid is back", room.pipeline.grid.locate("A1") == (50, 50) and room.chaos.active is None
    )
    room.chaos.paid("quake", "fan1", room.now)  # Super Chats: no cooldown, in turn
    room.chaos.paid("flip", "fan2", room.now)
    first = room.chaos.active
    result.check(
        "a Super Chat's chaos starts at once, cooldown or not",
        first is not None and first.modifier.key == "quake",
    )
    await room.wait(MODIFIERS["quake"].seconds + 1)
    second = room.chaos.active
    result.check(
        "the next one follows when it ends",
        second is not None and (second.modifier.key, second.by) == ("flip", "fan2"),
    )
    del people


async def amendments(room: Room, result: Result) -> None:
    people = room.settled(30)
    room.say("u0", "!amend democracy_window 20")
    room.each(people[1:6], "!yes", (1, 80))  # 6 yes, 4 no: 60 %
    room.each(people[6:10], "!no", (1, 80))
    await room.wait(91)
    first = room.votes[0]
    result.check("60 % can't amend a law (75 % needed)", first.phase == "failed", (first.yes, first.no))
    result.check("the law didn't change", room.referee.laws.democracy_window_s == 12)
    room.now += 3601  # !amend's cooldown: an hour, chat talking all along
    room.settled(30)
    room.say("u10", "!amend democracy_window 20")
    room.each(people[11:22], "!yes", (1, 80))  # 12 yes, 2 no
    room.each(people[22:24], "!no", (1, 80))
    await room.wait(91)
    second = room.votes[-1]
    result.check("12 to 2 passes to the veto countdown", second.phase == "veto", (second.yes, second.no))
    room.each(people[24:26], "!veto", (1, 30))  # 2 vetoes: 4 needed
    await room.wait(61)
    result.check("2 vetoes don't stop it", second.phase == "done", second.result)
    result.check(
        "Amendment #1: the law changed",
        room.referee.laws.democracy_window_s == 20 and second.result.startswith("Amendment #1"),
        second.result,
    )
    result.check("another amendment waits an hour", "just voted on" in room.say("u26", "!amend lives 4"))
    room.now += 3601
    room.settled(30)
    for text, what, why in (
        ("!amend democracy_window 99", "out of bounds", "democracy_window can be 8 to 30 s"),
        ("!amend newrun_threshold 50", "!newrun's rules", UNKNOWN),
        ("!amend quorum 2", "an unknown law", UNKNOWN),
    ):
        votes = len(room.votes)
        reply = room.say("u27", text)
        result.check(f"refused before any vote: {what}", reply == why and len(room.votes) == votes, reply)


async def night(room: Room, result: Result) -> None:
    room.night.set("auto", "the simulator")
    people = room.settled(12)
    room.arrive(["mod"], 600, {"moderator"})
    room.say("mod", "evening, chat")
    room.play(people, 17 * 60)  # chat plays all along
    await room.wait(10 * 60)
    result.check("a moderator 10 minutes ago: still day", not room.night.on and not room.nights)
    result.check("by day a !rewind needs 3 yes of 12", room.referee.laws.tier("rewind").quorum(12) == 3)
    await room.wait(5 * 60 + 1)
    result.check("15 minutes without a moderator: night falls", room.night.on, room.nights)
    result.check("democracy only", room.referee.democracy)
    result.check("chat can't push the meter", room.say("u0", "!anarchy") == NIGHT_MODE)
    result.check("u1 proposes !rewind", room.say("u1", "!rewind") == "ok")
    vote = room.votes[-1]
    result.check("at night it needs 6 yes", vote.quorum == 6, vote.quorum)
    room.each(people[2:6], "!yes", (1, 20))  # 5 yes in all
    await room.wait(31)
    result.check("5 yes aren't enough at night", vote.phase == "failed", vote.result)
    [late] = room.settled(1, "late")
    replies = await room.burst(late, "!click F7", 3)
    result.check(
        "commands come slower: a burst of 2, then one every 4 s",
        replies == ["ok", "ok", "slow down: one command every 4 seconds"],
        replies,
    )
    room.say("mod", "back!")
    result.check(
        "a moderator speaks: the day is back",
        not room.night.on and room.nights[-1] == Change(False, "mod is here"),
    )
    result.check("anarchy, as before the night", not room.referee.democracy)
    [early] = room.settled(1, "early")
    replies = await room.burst(early, "!click F7", 3)
    result.check("commands at the day's pace again", replies == ["ok"] * 3, replies)


def _a_listed_word() -> str:
    """A word the text filter refuses, read from its own lists."""
    for path in sorted(LISTS.glob("*.txt")):
        for line in path.read_text("utf-8").splitlines():
            word = line.strip().removeprefix("*")
            if word and not word.startswith(("#", "!")) and word.isascii() and word.isalpha():
                return word
    raise RuntimeError(f"no word lists in {LISTS}")


Scenario = Callable[[Room, Result], Awaitable[None]]
SCENARIOS: dict[str, Scenario] = {
    "fifty": fifty,
    "split": split,
    "brigade": brigade,
    "veto": veto,
    "low population": low_population,
    "ties": ties,
    "macros": macros,
    "driver": driver,
    "chaos": chaos,
    "amendments": amendments,
    "night": night,
}


async def run(name: str, seed: int = 1) -> Result:
    """One scenario in a room of its own."""
    result = Result(name)
    with Room(seed) as room:
        await SCENARIOS[name](room, result)
    return result


def run_all(seed: int = 1) -> list[Result]:
    async def go() -> list[Result]:
        return [await run(name, seed) for name in SCENARIOS]

    return asyncio.run(go())
