"""The constitution (PLAN.md 6.10): what chat may change with `!amend`, and within what.

`!amend LAW VALUE` is a vote (6.4: 75 % yes, a quorum of 30 % of those active
and at least 4, 90 s, a 60 s veto countdown, an hour before the next). Chat
names a law by its short name:

    democracy_window   seconds a democracy window lasts            8 to 30
    snapshot_every     minutes between automatic snapshots          10 to 30
    driver_every       minutes between driver's seats              5 to 30
    driver_time        seconds a driver drives                     30 to 120
    democracy_at       where on the meter democracy begins          70 to 90
    anarchy_at         where on the meter anarchy comes back        10 to 30
    lives              lives a run starts with (from the next run)  1 to 5
    VOTE_threshold     % of yes !VOTE needs, for reboot, save, reset, rewind
                       and load: within 10 points of the ruleset's own

Never chat's ("the Bill of Rights"): the filters and blocklists, rate limits,
the network's rules, bans, panic and moderators' powers, !newrun's rules, who
may vote, and the !amend vote itself.

A passed amendment is numbered (#1, #2…), kept in the store with who proposed
it, and written in the laws table over the ruleset, as an operator's `law` is
(referee.Referee.amend). Refusals use fixed wording: never what chat typed.
"""

from dataclasses import dataclass

from chatuses.referee.laws import Laws, Ruleset, RulesetError, apply

THRESHOLD_VOTES = ("reboot", "save", "reset", "rewind", "load")  # tiers 1 to 3
THRESHOLD_SPAN = 10.0  # points either side of the ruleset's own threshold
UNKNOWN = "chat can't amend that: the site's Laws page lists what it can"


class AmendError(RulesetError):
    """Why an amendment can't be: fixed wording, for chat."""


@dataclass(frozen=True)
class Clause:
    name: str  # chat's: democracy_window
    key: str  # the laws table's: democracy_window_s, or tiers.rewind.threshold
    what: str  # for people: "seconds a democracy window lasts"
    low: float
    high: float
    unit: str = ""  # "s", "min", "%"
    percent: bool = False  # chat writes a percentage; the law is a fraction

    def law(self, value: float) -> float:
        return value / 100 if self.percent else value

    def chat(self, law: float) -> float:
        """The law's value as chat writes it."""
        return round(law * 100, 2) if self.percent else law

    def bounds(self) -> str:
        return f"{self.low:g} to {self.high:g}{' ' + self.unit if self.unit else ''}"


_FIXED = (
    Clause("democracy_window", "democracy_window_s", "seconds a democracy window lasts", 8, 30, "s"),
    Clause("snapshot_every", "auto_snapshot_min", "minutes between automatic snapshots", 10, 30, "min"),
    Clause("driver_every", "driver_interval_min", "minutes between driver's seats", 5, 30, "min"),
    Clause("driver_time", "driver_duration_s", "seconds a driver drives", 30, 120, "s"),
    Clause("democracy_at", "meter_democracy_at", "where on the meter democracy begins", 70, 90),
    Clause("anarchy_at", "meter_anarchy_at", "where on the meter anarchy comes back", 10, 30),
    Clause("lives", "lives_start", "lives a run starts with (from the next run)", 1, 5),
)


def clauses(ruleset: Ruleset) -> dict[str, Clause]:
    """What chat may amend under this ruleset, by chat's name."""
    found = {c.name: c for c in _FIXED}
    for vote in THRESHOLD_VOTES:
        base = ruleset.laws.tier(vote).threshold * 100
        low, high = max(50.0, base - THRESHOLD_SPAN), min(100.0, base + THRESHOLD_SPAN)
        name = f"{vote}_threshold"
        found[name] = Clause(
            name, f"tiers.{vote}.threshold", f"% of yes !{vote} needs", round(low), round(high), "%", True
        )
    return found


def current(clause: Clause, laws: Laws) -> float:
    """The law's value now, as chat writes it."""
    if clause.key.startswith("tiers."):
        _, vote, setting = clause.key.split(".")
        return clause.chat(float(getattr(laws.tier(vote), setting)))
    return clause.chat(float(getattr(laws, clause.key)))


def check(ruleset: Ruleset, laws: Laws, name: str, value: float) -> tuple[Clause, float]:
    """The clause and the law's new value; AmendError (fixed wording) if chat can't."""
    clause = clauses(ruleset).get(name)
    if clause is None:
        raise AmendError(UNKNOWN)
    if not clause.low <= value <= clause.high:
        raise AmendError(f"{clause.name} can be {clause.bounds()}")
    if not clause.percent and not float(value).is_integer():
        raise AmendError(f"{clause.name} takes a whole number")
    if current(clause, laws) == value:
        raise AmendError(f"{clause.name} is {value:g} already")
    law = clause.law(value)
    try:
        apply(laws, {clause.key: int(law) if not clause.percent else law})  # the laws' own bounds too
    except RulesetError as err:
        raise AmendError(f"{clause.name} can't be {value:g} ({err})") from err
    return clause, law
