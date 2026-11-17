# Phase 9 — Depth

PLAN.md Phase 9, in this order: macros, the constitution (amendments), the
driver's seat with impeachment, chaos modifiers with the points shop, and
clicking on text. Done when each has simulator coverage and was live-tested
in at least one session.

1. **Macros** (below).
2. **The constitution: `!amend`** (below).
3. **The driver's seat and `!impeach`** (below).
4. Chaos modifiers and the points shop.
5. `!click "text"`.

## Step 1: macros

PLAN.md 6.10. Chat names a sequence of input commands and votes it in:

```
!macro term = key ctrl+alt+t
!macro hello = type hello world; enter
```

Then anyone types `!term` or `!hello`. `!unmacro NAME` votes one out.

**The grammar.** A name is 2 to 12 letters and digits, starting with a
letter, and can't be a command's (`!click`, `!rewind`, `!amend`…, and the
ones coming: `!buy`, `!chaos`, `!wait`). The commands are separated by `;`,
at most 8, each an input command as chat would type it (`type`, `key`,
`click`, `move`, `drag`, `scroll`, the keys), the `!` optional, with all of
their usual limits. No macro inside a macro, no vote inside one. A refusal
says which command of the macro is wrong, by its number, never repeating
what chat typed.

**The vote.** `!macro` and `!unmacro` are power votes (PLAN.md 6.4): 50 %
yes and a quorum of 15 % of those active (2 at least), a minute to vote,
no veto countdown, 5 minutes before another of the same (a ruleset can
change that, like any vote's tier, or turn them off). A macro can't take a name in use, and
there are 64 at most: `!unmacro` one first. The text filter checks the
name and every typed text when it's proposed (the vote panel shows it),
and the typed text again, with what was typed just before, each time it
runs: a macro can't launder a slur.

**Running one.** A macro is one command: it rate-limits like one, it's one
line on the overlay (`!hello`), and its commands run one after the other,
0.2 s apart, with nobody else's input in between. In democracy, `!hello` is
a ballot like any command. A macro can't wait for a window to open: keep
`key ctrl+alt+t` and what's typed in it as separate macros.

Macros are kept in brain's store and outlive runs and restarts: chat's
language grows. Each is parsed again when brain starts; one that no longer
parses (a grid that changed) is left out, and the journal says so. A
deletion request takes the proposer's ID off the macros they proposed, not
the macros. The site's Laws page lists them.

Moderators: `chat-brain-admin macros`, `chat-brain-admin macro remove
NAME` (no vote).

**Also in this step.** A refusal on stream can quote chat's word ("unknown
command !word"): any refusal the text filter would refuse is now replaced
on the overlay by fixed wording, so a slur typed as a command never shows.

### What was tested

- `tests/test_macros.py`: the grammar and 16 kinds of bad macro, with the
  reason; no macro in a macro; the filter on the name and text when
  proposed and the text when run, which it learns; the steps' input events
  in order, as one job; brain: voted in, run, kept across a restart,
  refused while it exists, voted out, at most 64, removed by a moderator,
  one that no longer parses left out, a deletion request, the site's list;
  a refusal quoting a slur screened before the overlay.
- `tests/test_parser.py`: macro refusals never repeat what chat wrote.
- The simulator's `macros` scenario (`python -m chatuses.sim --scenarios`):
  20 people vote one in, anyone runs it, it rate-limits as one command,
  four bad ones refused, a slur from the filter's own lists refused before
  any vote, a vote takes it out.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
admin macros                                 # No macros yet: …
```

In a session: `!macro term = key ctrl+alt+t`, 3 `!yes` from people who've
been around 2 minutes, then `!term` opens a terminal.

## Step 2: the constitution

PLAN.md 6.10 and 6.4. `!amend LAW VALUE` proposes changing one of the laws
chat may change, within bounds (`referee/constitution.py`):

| Chat writes | The law | Bounds |
|---|---|---|
| `democracy_window` | seconds a democracy window lasts | 8 to 30 |
| `snapshot_every` | minutes between automatic snapshots | 10 to 30 |
| `driver_every` | minutes between driver's seats (step 3) | 5 to 30 |
| `driver_time` | seconds a driver drives (step 3) | 30 to 120 |
| `democracy_at` | where on the meter democracy begins | 70 to 90 |
| `anarchy_at` | where on the meter anarchy comes back | 10 to 30 |
| `lives` | lives a run starts with, from the next run | 1 to 5 |
| `reboot_threshold`, `save_threshold`, `reset_threshold`, `rewind_threshold`, `load_threshold` | % of yes the vote needs | the ruleset's ± 10 points, never under 50 |

For example `!amend democracy_window 20` or `!amend rewind_threshold 70`.

Never chat's ("the Bill of Rights"): the filters and blocklists, rate
limits, the network's rules, bans, panic and moderators' powers, who may
vote (eligibility), `!newrun`'s rules, and `!amend`'s own.

**The vote** is the hardest there is (6.4): 75 % yes, a quorum of 30 % of
those active and at least 4, 90 seconds, then a 60-second veto countdown,
and an hour before the next amendment. What the constitution doesn't
allow is refused before any vote, in fixed words (`democracy_window can be
8 to 30 s`; an unknown law: `chat can't amend that: the site's Laws page
lists what it can`), never repeating what chat typed; a value the law has
already is refused too.

**A passed amendment** is numbered (`Amendment #3: democracy_window is 20
s`), kept with who proposed it, and written in the laws table over the
ruleset, as `chat-brain-admin law` writes (marked `amendment #3`). It
applies from the next vote or window; `lives` from the next run. A
moderator undoes one with `chat-brain-admin law KEY default`; the
amendment stays in the record. A deletion request takes the proposer off
it. The site's Laws page says, for each law, whether chat may amend it
and within what, and lists every amendment with its number, the law, the
value, when, and who proposed it (their name as the overlay shows it).

### What was tested

- `tests/test_constitution.py`: the clauses and their bounds (thresholds
  around the ruleset's, never under half; nothing for `!newrun` or
  `!amend`); eight kinds of amendment refused in fixed words; thresholds
  as percentages kept as fractions; the grammar, refusals never repeating
  chat; through brain: refused before a vote, passed through its veto
  countdown, numbered, applied, kept across a restart; one failing its
  quorum, one vetoed, one waiting the hour; `lives` from the next run and
  a threshold from the next vote; a deletion request; the site's page.
- The simulator's `amendments` scenario: 60 % fails (75 % needed), 12 of
  30 pass it through 2 vetoes, the law changes as Amendment #1, the next
  one waits an hour, out of bounds, `!newrun`'s rules and an unknown law
  refused before any vote.

### On the server

With step 1's: `sudo images/build.sh brain && sudo images/brain/deploy.sh
--replace`. In a session, with 5 or more people around: `!amend
democracy_window 20`, 4 `!yes`, wait out the 90 s and the veto countdown:
`admin laws` shows `democracy_window_s = 20`, and the site's Laws page
Amendment #1.

## Step 3: the driver's seat

PLAN.md 6.9, `control/src/chatuses/game/driver.py`.

**The offer.** Every `driver_interval_min` minutes (a law, 10; chat can
`!amend driver_every`), brain offers the seat to someone picked at random
among those who are active (a message in the last 5 minutes; never the
banned), have had 3 commands accepted this session (since `session.py
start`, or since brain started), and haven't been offered it in the last
hour. Not in democracy, nor while the machine is quiet (a panic, time
travel, a death, a run's end, the machine down): the next offer is then an
interval later. On Discord they're pinged in `#control` (only them: no
`@everyone`, no roles); the overlay shows a "Driver" panel and a line in
the log either way.

**The drive.** They have 15 seconds to take it with any input command,
which runs. Then for `driver_duration_s` seconds (60; `!amend
driver_time`) only their input reaches the machine: everyone else's is
refused with `NAME is driving: 42 s left` (once per person per few
seconds on stream). Votes, ballots and the meter work as ever. An offer
nobody takes runs out; so does the drive.

**`!impeach`** proposes ending it: 50 %, a quorum of 20 % of those active
(2 at least), 20 seconds, 5 minutes apart (6.4). With nobody in the seat
it's refused (`nobody is driving`). Democracy beginning, or a panic, ends
a drive at once.

The event log keeps each offer, take, end and impeachment with who (a
deletion request takes those entries). `admin status` has a `Driver's
seat:` line. A ruleset without `driver_seat` in its features never offers
it.

### What was tested

- `tests/test_driver.py`: an offer only after an interval, to someone with
  3 commands; taking it with a command, a 60-second drive, others refused
  with the time left, the drive running out; an offer running out, and no
  second offer within the hour; no offer when brain says not, the next an
  interval later; impeachment. Through brain: a Discord user pinged, the
  overlay's driver message, the status line, someone else's click refused,
  `!impeach` voted through, the events logged and forgotten on a deletion
  request; democracy and a panic ending a drive; `!impeach` refused with
  nobody driving.
- The simulator's `driver` scenario: after 10 minutes of play the seat
  goes to someone who played, never to someone with 2 commands; others
  still play while it's only offered; the driver's command takes it, a
  60-second drive, others refused, the meter still works; `!impeach`
  passes and ends it; everyone plays again; the next offer goes to someone
  else, and runs out when nobody answers.
- The overlay's Driver panel in Chromium from a fake feed: offered (any
  command takes the wheel), driving (the countdown, `!impeach`), hidden.

### On the server

`sudo images/build.sh brain && sudo images/brain/deploy.sh --replace`, then
`sudo images/build.sh bcast && sudo images/bcast/deploy.sh --replace` for
the overlay's Driver panel (between streams: it restarts the stream). In a
session, after 10 minutes of play: someone gets the seat (a Discord user
is pinged); `admin status` shows `Driver's seat: NAME drives, … s left`.
