# Phase 6 — Governance and time travel

PLAN.md Phase 6: chat votes on power actions and time travel, switches
between anarchy and democracy, and rulesets set the numbers. Five steps,
each tested before the next:

1. **Rulesets, laws and power votes** (below).
2. **Snapshots and power** (below): auto snapshots, named saves, `!rewind`, `!load`,
   `!reboot`, `!reset`, the overlay's timeline.
3. **Democracy mode** (below): the meter, 12-second ballots, tally bars.
4. The Discord control panel: Yes / No / Veto / Anarchy / Democracy buttons.
5. Simulator scenarios (50 users, split votes, brigades, low population,
   ties) and the end-to-end test.

## Step 1: rulesets, laws and power votes

### Rulesets and laws

A ruleset is `rulesets/NAME.yaml` (PLAN.md 6.12); brain reads them from
`/usr/lib/chat-brain/rulesets`, and starts with `roguelike`. Anything a
ruleset leaves out keeps PLAN.md's numbers (6.3, 6.4, 6.6). Every law has
bounds, and a ruleset with an unknown key, a value out of bounds or the
wrong type is refused whole, with the line that's wrong: a typo never
leaves a law silently at its default (`control/src/chatuses/referee/laws.py`).

```yaml
laws:
  democracy_window_s: 12          # a law
tiers:
  rewind: { window_s: 30, veto_s: 30, cooldown_min: 10 }   # a vote's settings
commands:
  disabled: [newrun]              # commands chat can't use
```

On top of the ruleset, a law can be changed while running; the change is
kept in the store (the `laws` table) until put back:

```bash
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin ruleset                      # the one in use, and those there are
admin ruleset roguelike            # switch (votes opened from then on)
admin law tiers.reboot.window_s 45 # within its bounds (10 to 300)
admin laws                         # what's changed
admin law tiers.reboot.window_s default
```

Chat's own `!amend` comes with Phase 9.

### Power votes

`!reboot`, `!reset`, `!save NAME`, `!rewind [N]`, `!load NAME` open a vote
(PLAN.md 6.4); `!yes` and `!no` vote, `!veto` cancels during a countdown.

| Vote | Passes with | Quorum (of active users, at least) | Window | Veto countdown | Cooldown |
|---|---|---|---|---|---|
| `!reboot`, `!save` | 50 % yes | 15 %, 2 | 30 s | — | 2 min |
| `!reset` | 60 % | 20 %, 3 | 30 s | — | 5 min |
| `!rewind`, `!load` | 66 % | 25 %, 3 | 30 s | 30 s | 10 min |

- **One vote at a time**; the proposer's yes counts.
- **Who may vote** is fixed when the vote opens: people who sent a message
  in the 5 minutes before **and** have been around for 2 minutes (6.5). A
  raid of new accounts can't vote (they still count as active, so they
  raise the quorum), and neither can people who weren't around. Proposing
  needs the same 2 minutes.
- **Veto:** a passed `!rewind` or `!load` waits 30 s; it's cancelled if
  vetoes reach 3, or a third of the yes votes when that's more.
- **Cooldown** after a vote, whatever its result: the same action can't be
  proposed again for a while.
- **Moderators** end a vote with `/endvote` (Discord), the dashboard's *End
  vote*, or `chat-brain-admin endvote`. A panic ends the vote running, and
  no vote can open during one.
- `!newrun` (runs, Phase 8) and `!impeach` (the driver's seat, Phase 9)
  are refused for now, saying so.

The overlay's sidebar shows the vote: what, who proposed it, the time left,
yes and no against what's needed, then how it ended. Ballots (`!yes`, `!no`,
`!veto`) are counted there rather than each put in the command log, which a
busy vote would otherwise flood; a refused one still shows, once per person
every 10 s. The dashboard shows it
too, with the ruleset. Votes are kept in the store (`votes`); who proposed
one is erased after 90 days or on a deletion request, like everything else.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo images/brain/check.sh
```

The check's new **Referee** lines should pass: the ruleset in use and the
rulesets in the image.

To try a vote, two or more people in chat (YouTube or Discord) who've each
sent a message, waited 2 minutes, and spoken again in the last 5 minutes:
one writes `!reboot`, another `!yes`. The sidebar counts down 30 s, then
says the vote passed (since step 2, the machine reboots). On your own, two
console names work the same way (`chat-brain-say --as alice '!reboot'`).

### What was tested

- `tests/test_laws.py`: the shipped ruleset is PLAN.md's; partial tiers;
  17 kinds of bad ruleset refused whole; odd names, missing and huge files.
- `tests/test_votes.py`, on a fake clock: passing and carrying out, quorum
  growing with the crowd, the threshold on yes and no, a tie at 50 %,
  eligibility fixed at opening (newcomers, lurkers, banned users), the last
  ballot counting, one vote at a time, cooldowns, veto countdowns both
  ways, moderators ending a vote, the overlay's view.
- `tests/test_brain.py`, `test_dashboard.py`, `test_moderation.py`,
  `test_discord.py`: votes through brain and on the overlay feed, a panic
  ending the vote, rulesets and laws from the admin console, `/endvote` on
  Discord and the dashboard.
- The overlay's vote panel, rendered in Chromium from a fake feed: open,
  veto countdown, failed.

## Step 2: snapshots, time travel and power

What a passed vote does (`control/src/chatuses/snapshots.py`, PLAN.md 6.6):

| Vote | What happens |
|---|---|
| `!save NAME` | a named save, `save-NAME-<time>` on the target; 3 slots: the same name replaces it, a new one in a full set the oldest |
| `!rewind`, `!rewind N` | back to the latest auto snapshot, or the N-th back |
| `!load NAME` | back to that save |
| `!reboot` | a clean reboot (30 s to shut down; if it doesn't, chat is told `!reset` forces it) |
| `!reset` | a hard reset |

- **Auto snapshots** every 15 minutes (`auto_snapshot_min`, 10 to 30),
  only while the target is healthy (input up, the watchdog happy, no panic)
  and its screen changed since the last one; a ring of 8
  (`auto_snapshot_keep`).
- **Before a vote even opens**, brain says if it can't happen: no snapshot
  that far back, no save by that name, a rewind already under way.
- **During time travel** (and reboots) input is frozen; commands queued
  for the screen before are dropped, then input comes back (unless a
  panic started meanwhile). The desktop reconnects by itself: Incus
  restarts the container to restore it, in about a second.
- **Moderators** go back to any of chat's snapshots without a vote:
  `/rewind-to SNAPSHOT` on Discord (its number, or a save's name), *Rewind
  to* on the dashboard's new Snapshots list, or `chat-brain-admin rewind-to
  N`. An open vote ends; `#mod-alerts` says when it's done.
- **Thumbnails** (200 px) are taken with each snapshot, kept in
  `/var/lib/chat-brain/thumbs` (on brain's data volume, D53) and served to
  the overlay at `/thumbs/<number>.png`: they are past frames of the
  stream, like the rest of the feed (D37). They go with their snapshot.
- **The overlay's "Time travel" panel** shows the save slots (with names)
  and the 3 latest auto snapshots, captioned `!rewind`, `!rewind 2`,
  `!rewind 3`: the six thumbnails of PLAN.md's timeline, labelled with
  what to type to reach them.
- **When brain starts** it forgets snapshots the target no longer has (it
  was rebuilt, or someone deleted them), so chat is never offered one that's
  gone.
- Forensic snapshots taken on panic (`panic-…`) are never chat's: they
  aren't in the list.

```bash
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin snapshot            # take an auto snapshot now (to try things)
admin snapshots           # chat's snapshots: number, kind, label, Incus name
admin rewind-to 3         # go back to #3 (or a save's name), no vote
```

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo images/brain/check.sh
```

Then, with two or three people in chat (or console names) settled in:
`admin snapshot`, then `!save clean` and `!yes`: the slot fills with its
thumbnail. Type something on the desktop, then `!rewind` and enough
`!yes`: after the vote and its 30-second veto countdown, the desktop drops
for a second and comes back as it was. `!load clean` likewise.

### What was tested

- `tests/test_snapshots.py`: the ring, save slots (same name, oldest),
  rewind targets, finding by number or name, two snapshots in one second,
  restore, reconcile after a rebuild, failed deletes and thumbnails, a
  failed snapshot not recorded, the overlay's view.
- `tests/test_incus.py`: listing, restore, delete, restart (graceful and
  forced) send what Incus expects; a refused restore raises.
- `tests/test_brain.py`: `!save`, `!rewind`, `!load` votes end to end on a
  fake Incus (input frozen during, back after); a reboot the machine
  ignores tells chat to `!reset`; no Incus, no time travel; refusals before
  a vote opens; a moderator's rewind; when auto snapshots are due;
  thumbnails served and nothing else.
- `tests/test_dashboard.py`, `test_moderation.py`, `test_discord.py`: the
  Snapshots list and *Rewind to*, `/rewind-to`.
- **On a real Incus (6.0, the sandbox),** through brain's own client with a
  restricted certificate: snapshot, list, restore (0.8 s; a file written
  after the snapshot is gone), delete, reset (0.8 s); a reboot of a
  container whose init ignores the shutdown signal fails after 30 s (so
  brain's wording). The snapshot manager on it: the ring, a save restored,
  thumbnails kept with their snapshots, reconcile after a deletion behind
  brain's back. Snapshot names with `_` and suffixes are accepted.
- The overlay's Time travel panel rendered in Chromium from a fake feed,
  with and without a vote running: it fits the sidebar.

## Step 3: anarchy and democracy

`control/src/chatuses/referee/modes.py`, PLAN.md 6.3.

- **Anarchy** (the default): every valid command runs, in order.
- **Democracy:** commands are ballots. The first one opens a 12-second
  window (`democracy_window_s`); identical commands are tallied (as the
  log writes them: `!click F7` is `!click F7` however it was typed), each
  person's latest ballot counting once, and the most voted runs as
  "democracy (votes/ballots)". A tie goes to the earliest.
- **The meter** (0 to 100, from 50): `!democracy` pushes it up 2,
  `!anarchy` down 2, once per person every 10 s. At 80 the mode becomes
  democracy, at 20 anarchy again; after a switch it stays for 2 minutes
  whatever the meter does. It drifts back toward 50, a point every 10 s.
  Every number is a law (`meter_step`, `meter_democracy_at`,
  `meter_anarchy_at`, `meter_push_gap_s`, `mode_lock_s`, `meter_drift_s`).
- **Who pushes the meter:** like voters, people who've been around for 2
  minutes (and spoke in the last 5): a raid can't flip the mode. Ballots in
  democracy are ordinary commands, open to anyone whose command is
  accepted (lockdown is the answer to a raid there).
- **Moderators** set the mode at once: `/mode anarchy|democracy` on
  Discord, *Switch to …* on the dashboard, `chat-brain-admin mode
  democracy`. It's then locked for 2 minutes like any switch.
- A panic, a rewind or a reboot drops the window's ballots: they were for
  the screen before.
- The mode lives in brain: a restart begins in anarchy, the meter at 50.
- A ruleset without `democracy` in its features turns the meter off.

**On stream:** the sidebar's meter moves (thresholds marked), with the mode
and its lock below. In democracy, the bottom strip shows the commands being
voted, with their counts and the seconds left, instead of the log; the
ticker says how to push back.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin mode democracy
```

Then two or three people send commands: the strip shows the tally, and
every 12 seconds the winner runs. `admin mode anarchy` (or 15 `!anarchy`
from settled people) puts it back.

### What was tested

- `tests/test_modes.py`, on a fake clock: pushes and the per-person gap,
  newcomers can't push, hysteresis and the lock, drift both ways, the most
  voted command with latest-ballot-counts and earliest-wins ties, `/mode`
  and clearing, the overlay's view.
- `tests/test_brain.py`: a push from a settled user, a moderator's switch,
  three people's clicks tallied (no log lines, nothing queued), the winner
  reaching a fake VNC desktop when the window closes; `test_dashboard.py`:
  switching from the dashboard; `test_discord.py`: `/mode`.
- The overlay in both modes rendered in Chromium: meter, mode and lock in
  the sidebar (with a vote and the time travel panel, it fits), the tally
  in the strip.
