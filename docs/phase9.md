# Phase 9 — Depth

PLAN.md Phase 9, in this order: macros, the constitution (amendments), the
driver's seat with impeachment, chaos modifiers with the points shop, and
clicking on text. Done when each has simulator coverage and was live-tested
in at least one session.

1. **Macros** (below).
2. **The constitution: `!amend`** (below).
3. The driver's seat and `!impeach`.
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
