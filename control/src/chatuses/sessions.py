"""Stream sessions (PLAN.md Phase 7): what each beta or scheduled session was like.

`chat-brain-admin session start NAME`, then `session end` (infra/host/session.py
does both, and adds what only the host sees). While a session runs, brain
notes every SAMPLE_EVERY seconds how many are active in chat (spoke in the
last 5 minutes, per platform). Not YouTube's concurrent viewers: YouTube's
rules let a statistic be kept 30 days only, and YouTube Studio has them
(older sessions may hold some, until store.expire_youtube clears them). At
the end it adds up, from the store, the session's commands, votes, panics,
content holds and screen OCR hits, keeps the report and posts it in #ops.

Only counts are kept, never who: a session's report outlives the 90-day
logs (PLAN.md 11). A session survives a brain restart (it's in the store);
the minutes brain was down have no samples.

The encoder's speed and the containers' CPU are bcast's and the host's:
infra/host/session.py adds them (brain can't reach bcast, D37).
"""

import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from chatuses.identity import Presence
from chatuses.store import Store

SAMPLE_EVERY = 60.0  # seconds between samples
PLATFORMS = {"yt": "YouTube", "dc": "Discord", "console": "console"}
VOTE_ENDS = ("done", "failed", "vetoed", "cancelled", "error")


def active_by_platform(presence: Presence, now: float) -> dict[str, int]:
    """People active in chat now (a message in the last 5 minutes), by platform."""
    counts = Counter(user.partition(":")[0] for user in presence.active(now))
    return dict(sorted(counts.items()))


class Sessions:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    @property
    def current(self) -> dict[str, Any] | None:
        return self.store.running_session()

    def start(self, name: str) -> tuple[bool, str]:
        name = " ".join(name.split())[:80]
        if not name:
            return False, "A session needs a name: session start NAME"
        if (running := self.current) is not None:
            return False, f"Session {running['name']!r} is running: end it first (session end)."
        self.store.open_session(name, self.clock())
        return True, (
            f"Session {name!r} started: brain notes who's active in chat every minute."
            " `session end` when it's over."
        )

    def sample(self, viewers: int | None, active: dict[str, int]) -> None:
        if (running := self.current) is not None:
            self.store.add_session_sample(running["id"], [round(self.clock()), viewers, active])

    def end(self) -> tuple[bool, str, dict[str, Any] | None]:
        running = self.current
        if running is None:
            return False, "No session running.", None
        now = self.clock()
        report = build_report(
            running["name"],
            running["started"],
            now,
            running["samples"],
            self.store.session_numbers(running["started"], now),
        )
        self.store.close_session(running["id"], now, report)
        return True, render(report), report

    def status(self) -> str:
        running = self.current
        if running is None:
            return "No session running."
        minutes = round((self.clock() - running["started"]) / 60)
        return f"Session {running['name']!r}: {minutes} min, {len(running['samples'])} samples."


def build_report(
    name: str, started: float, ended: float, samples: list[list[Any]], numbers: dict[str, Any]
) -> dict[str, Any]:
    """The session's numbers, from its samples and the store's (Store.session_numbers)."""
    minutes = max(0.0, ended - started) / 60
    viewers = [s[1] for s in samples if isinstance(s[1], int)]
    actives = [sum(s[2].values()) for s in samples]
    peaks: dict[str, int] = {}
    for sample in samples:
        for platform, n in sample[2].items():
            peaks[platform] = max(peaks.get(platform, 0), n)
    outcomes: dict[str, int] = numbers["outcomes"]
    total = sum(outcomes.values())
    refused = {k: v for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1]) if k != "accepted"}
    by_kind: dict[str, list[int]] = {}
    results: Counter[str] = Counter()
    for kind, result in numbers["votes"]:
        counts = by_kind.setdefault(kind, [0, 0])
        counts[0] += 1
        counts[1] += result == "done"
        results[result or "open"] += 1
    panics = holds = confirmed = resumed_holds = ocr = 0
    covered = 0.0
    ocr_modes: Counter[str] = Counter()
    time_travel = power = 0
    for kind, payload in numbers["events"]:
        match kind:
            case "panic" if payload.get("from_hold"):
                confirmed += 1
            case "panic":
                panics += 1
            case "hold":
                holds += 1
            case "resume":
                covered += float(payload.get("seconds") or 0)
                resumed_holds += payload.get("kind") == "hold"
            case "ocr_hit":
                ocr += 1
                ocr_modes[str(payload.get("mode") or "?")] += 1
            case "time-travel":
                time_travel += 1
            case "power":
                power += 1
            case _:
                pass
    return {
        "name": name,
        "started": round(started),
        "ended": round(ended),
        "minutes": round(minutes, 1),
        "viewers": {
            "peak": max(viewers) if viewers else None,
            "average": round(sum(viewers) / len(viewers), 1) if viewers else None,
        },
        "active": {
            "peak": max(actives) if actives else 0,
            "average": round(sum(actives) / len(actives), 1) if actives else 0.0,
            "peaks": dict(sorted(peaks.items())),
        },
        "samples": len(samples),
        "people": dict(sorted(numbers["people"].items())),
        "commands": {
            "total": total,
            "per_minute": round(total / minutes, 1) if minutes else 0.0,
            "accepted": outcomes.get("accepted", 0),
            "refused": refused,
        },
        "votes": {
            "proposed": sum(results.values()),
            **{r: results[r] for r in VOTE_ENDS if results[r]},
            "by_kind": dict(sorted(by_kind.items())),
        },
        "panics": panics,
        "holds": {"count": holds, "confirmed": confirmed, "resumed": resumed_holds},
        "covered_minutes": round(covered / 60, 1),
        "ocr_hits": {
            "count": ocr,
            "modes": dict(sorted(ocr_modes.items())),
            "real": int(numbers.get("ocr_verdicts", {}).get("real", 0)),
            "false": int(numbers.get("ocr_verdicts", {}).get("false", 0)),
        },
        "time_travel": time_travel,
        "power": power,
    }


def _clock(ts: float) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts))


def _duration(minutes: float) -> str:
    whole = round(minutes)
    return f"{whole // 60} h {whole % 60:02d} min" if whole >= 60 else f"{whole} min"


def _platforms(counts: dict[str, int]) -> str:
    return ", ".join(f"{PLATFORMS.get(p, p)} {n}" for p, n in counts.items() if n)


def _alarms(n: int) -> str:
    return f"{n} false alarm" + ("" if n == 1 else "s")


def render(report: dict[str, Any]) -> str:
    """The report for people: #ops and chat-brain-admin."""
    v, a, c, votes, h = (
        report["viewers"],
        report["active"],
        report["commands"],
        report["votes"],
        report["holds"],
    )
    day = time.strftime("%Y-%m-%d", time.gmtime(report["started"]))
    lines = [
        f"📊 Session {report['name']!r}: {_duration(report['minutes'])} "
        f"({day}, {_clock(report['started'])} to {_clock(report['ended'])})"
    ]
    if v["peak"] is None:
        lines.append("Viewers (YouTube): in YouTube Studio (YouTube's numbers aren't kept here)")
    else:
        lines.append(f"Viewers (YouTube): peak {v['peak']}, average {v['average']}")
    peaks = f" ({_platforms(a['peaks'])} at their peaks)" if a["peaks"] else ""
    lines.append(f"Active in chat: peak {a['peak']}, average {a['average']}{peaks}")
    people = report["people"]
    lines.append(
        f"People who sent commands: {sum(people.values())}" + (f" ({_platforms(people)})" if people else "")
    )
    refused = sum(c["refused"].values())
    why = ", ".join(f"{k} {n}" for k, n in c["refused"].items())
    lines.append(
        f"Commands: {c['total']} ({c['per_minute']} a minute): {c['accepted']} accepted, {refused} refused"
        + (f" ({why})" if why else "")
    )
    if votes["proposed"]:
        kinds = ", ".join(f"{k} {p} of {n}" for k, (n, p) in votes["by_kind"].items())
        ends = ", ".join(f"{votes[r]} {r}" for r in VOTE_ENDS if r != "done" and votes.get(r))
        lines.append(
            f"Votes: {votes['proposed']} proposed, {votes.get('done', 0)} carried out ({kinds})"
            + (f"; {ends}" if ends else "")
        )
    else:
        lines.append("Votes: none")
    ocr = report["ocr_hits"]
    unmarked = ocr["count"] - ocr["real"] - ocr["false"]
    marked = f" ({ocr['real']} real, {_alarms(ocr['false'])}, {unmarked} not marked)" if ocr["count"] else ""
    lines.append(
        f"Panics: {report['panics']}; content holds: {h['count']} ({h['confirmed']} confirmed as panics, "
        f"{_alarms(h['resumed'])} resumed); covered {report['covered_minutes']} min in all"
    )
    lines.append(f"Screen OCR hits: {ocr['count']}{marked}")
    lines.append(f"Time travel: {report['time_travel']}, reboots and resets: {report['power']}")
    return "\n".join(lines)
