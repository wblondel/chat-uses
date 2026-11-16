# Phase 6 — Governance and time travel

PLAN.md Phase 6: chat votes on power actions and time travel, switches
between anarchy and democracy, and rulesets set the numbers. Five steps,
each tested before the next:

1. **Rulesets, laws and power votes** (below).
2. **Snapshots and power** (below): auto snapshots, named saves, `!rewind`, `!load`,
   `!reboot`, `!reset`, the overlay's timeline.
3. **Democracy mode** (below): the meter, 12-second ballots, tally bars.
4. **The Discord control panel** (below): Yes / No / Veto / Anarchy / Democracy buttons.
5. **Simulator scenarios and the end-to-end test** (below): 50 users, split
   votes, brigades, low population, ties; then the stream with friends.

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

## Step 4: the Discord control panel

`control/src/chatuses/adapters/discord_panel.py`, PLAN.md D9.

The bot keeps one message in `#control`: what's going on, and five buttons,
**Yes**, **No**, **Veto**, **Anarchy** and **Democracy**.

- **A press is typing.** Pressing *Yes* is the presser sending `!yes` in
  `#control`: the same Discord identity and roles, bans, lockdown, voting
  rules (being around 2 minutes before the vote opened) and meter gap. The answer
  ("You voted yes.", "Votes count from people who were here 2 minutes
  before it opened.") is private: only the presser sees it, and refusals
  aren't put on stream, since they already got the answer.
- **What it shows:** the mode, the meter as a bar with its thresholds and
  when a switch is allowed again; the vote running, its count, what it
  needs and when it ends; a panic. Countdowns are Discord timestamps
  ("in 25 seconds"), which tick in each viewer's app without edits.
  Buttons that can't do anything now are greyed out: *Yes* and *No* only
  while a vote is open, *Veto* only during a veto countdown, the mode
  buttons only if the ruleset has democracy.
- **One message.** It's edited in place when what it shows changes, at
  most every 2 seconds (Discord limits edits); nothing is posted per vote.
  When chat buries it (15 messages under it) it moves to the bottom: posted
  again, the old one deleted, at most once a minute, so it doesn't jump
  away from someone about to press. If someone deletes it, it comes back.
- **Restarts:** at start, panels an earlier run left in the last 100
  messages are deleted and a fresh one is posted. A clean stop turns it
  into "Offline" with its buttons greyed out. Every panel has the same
  button IDs and the bot listens for them from the start, so a press on any
  of them works.
- `chat-brain-admin status` says how it's doing: `Discord: connected,
  panel shown`, or the error (a missing permission, for example).

The bot needs nothing new: it already sends, embeds links and reads the
history in `#control`, and deleting its own messages takes no permission.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo incus exec brain --project infra -- chat-brain-admin status   # Discord: connected, panel shown
```

Then in `#control`: the panel is the bot's last message. Someone who's
been around a few minutes proposes `!reboot`; *Yes* and *No* light up and the count moves within 2 seconds of each press. Push
*Democracy* a few times from different accounts: the bar moves.

### What was tested

- `tests/test_discord_panel.py`: what the panel says and which buttons are
  live, for no vote, a vote, a veto countdown, a result, democracy with its
  lock, a panic, a ruleset without votes or democracy; names shown as
  written, not as markdown; the buttons and their IDs; old panels deleted at
  start (only the bot's), one posted, edited only on changes; moved when
  buried but not within a minute; posted again when deleted; an error
  reported, and the panel posted without history access; nothing sent
  while Discord is reconnecting, then the same message edited through the
  new connection; "Offline" on a clean stop.
- `tests/test_discord.py`: a press becomes the presser's `!yes` with their
  roles, answered privately; a press from another server is refused; the
  buttons are listened for from the start; messages under the panel are
  counted.
- `tests/test_brain.py`: presses vote, change a vote, are refused for a
  newcomer and by the meter's gap, push the meter, and refusals stay off
  the overlay; the panel's state follows the vote and a panic.
- The panel's real Discord payload built by discord.py (buttons, emoji,
  the embed), and a real button-press event dispatched through discord.py
  to brain and back as a private answer, with the server's roles mapped.
  It hasn't met the real Discord yet: that's the server check above.

## Step 5: simulator scenarios and the end-to-end test

### Governance scenarios

`control/src/chatuses/sim/governance.py`: crowds voting through the real
pipeline, referee and lockdown, on a simulated clock (a few minutes of
chat in a fraction of a second), routed as brain routes chat. Each one is
seeded and checks what happened; CI runs them all:

```bash
cd control && uv run python -m chatuses.sim --scenarios
```

| Scenario | What it shows |
|---|---|
| fifty | 50 people play while a `!rewind` vote runs: the quorum is a quarter of them (13); 36 yes and 10 no pass it (78%, 66% needed); 2 vetoes of the 12 needed don't stop it; it runs once; chat kept playing (hundreds of commands) |
| split | 30 yes and 20 no fail a `!rewind` (60%); it can't be proposed again for 10 minutes, `!reboot` can; 26 yes and 24 no pass the reboot (52%, half needed) |
| brigade | 60 accounts that just arrived spam while 20 regulars vote: every raider's ballot, meter push and proposal is refused; they raise the quorum (D54: 15% of 80 active, 12), so 8 yes fail. Two minutes later they could vote: under lockdown they're refused again (the quorum stays 12, they're still active), and the regulars' 12 yes pass |
| veto | 20 yes pass a `!rewind` to its countdown; 7 vetoes (a third of 20) stop it; someone who arrived after it opened can't veto |
| low population | 3 people: one alone can't `!save` (floor 2), two can't `!rewind` (floor 3), two reboot (67%) |
| ties | 15 pushes make it democracy; 5 to 5 goes to the command voted first; a changed ballot counts once (6 to 4); a `!reboot` split 10 to 10 passes, since the threshold is "at least half" |

What they pin down, worth knowing on stream (D58):

- **A raid can't pass anything, but can stall votes.** New accounts count
  as active, so they raise the quorum (D54) without being able to vote. A
  big raid makes power votes fail until it goes quiet (5 minutes) or
  moderators ban it. Lockdown keeps raiders' votes out but not their
  presence, so it doesn't lower the quorum.
- **Waiting 2 minutes makes anyone a voter.** Eligibility only stops
  accounts that arrive as a vote opens. If a raid stays, lockdown is the
  answer: regulars and people known for a day still vote.
- **A tie passes a 50% vote** (`!reboot`, `!save`): yes / (yes + no) ≥
  threshold. A ruleset that wants a strict majority sets the tier's
  threshold a little above 0.5 (0.51).

`tests/test_sim.py` runs each scenario with two seeds (40 seeds pass, too)
and checks that they catch a broken rule: with no settling time, the brigade
scenario fails.

### The end-to-end test (sandbox)

The real `Brain` and its real Incus client, with a restricted certificate,
against the sandbox's Incus 6.0 and its desktop container. Console users
voted through `Brain.handle`, as chat does; only the votes' clock was sped
up:

| Vote | Result |
|---|---|
| `!save base` | done in 1.5 s: snapshot `save-base-<time>` on the target |
| `!load base`, after changing a file | done in 2.4 s, after the veto countdown: the file as it was at the save |
| `!rewind`, after an auto snapshot and a change | done in 2.4 s: back to the auto snapshot |
| `!reboot` | the container's init ignores the shutdown signal: after 30 s, "the machine didn't shut down within 30 s: !reset forces it" |
| `!reset` | done in 0.8 s: init started again, the container running |

Every vote was in the store with its result. The real target (Phase 2's
desktop, with systemd) shuts down on the signal, so its `!reboot` should
work: that's part of the stream test below.

### On the server: the stream with friends

PLAN.md's "done when": the scenarios pass (CI), and rewind, reboot and
reset votes work on an unlisted stream with friends.

**First, the target on stream becomes `chatpc`** (D59). Until now the stream
showed Phase 2's test target, `phase2-check` at `.251`: its hostname said
so, it had the check's 2 GiB of memory instead of 8, and re-running the
check would have replaced it, with everything chat did and its snapshots.
`deploy.sh` launches the real one at `TARGET_IP` (10.30.0.10), named
`TARGET_NAME` (its hostname, which chat sees), with the target profile's
limits:

```bash
cd /opt/chat-uses && sudo git pull
sudo images/debian-xfce/deploy.sh      # chatpc at 10.30.0.10; it says if the image is older than the repo
sudo sed -i '/^TARGET_HOST=/d' /etc/chat-uses/bcast.local.env /etc/chat-uses/brain.local.env
sudo images/bcast/deploy.sh            # the stream shows chatpc
sudo images/debian-xfce/check.sh --instance chatpc
```

If `deploy.sh` says the desktop image is older than the repo, rebuild it
first (`sudo images/build.sh debian-xfce`) and run `deploy.sh --replace`.
`check.sh` now refuses to replace its test target while bcast or brain
point at it.

**Then Phase 6 as a whole** (steps 1 to 5) is one deploy of brain, which
finds `chatpc` by its address:

```bash
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo images/brain/check.sh
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin status        # Ruleset: roguelike; Discord: connected, panel shown
sudo incus delete -f phase2-check --project targets    # the old test target, once chatpc is on stream
```

Then, on an unlisted stream, with 3 or more friends who've each been in
chat (YouTube or Discord) for 2 minutes and spoken in the last 5:

1. `admin snapshot` (an auto snapshot), then someone types something on the
   desktop.
2. `!save test`, then enough `!yes` (2): the save slot fills with its
   thumbnail.
3. `!rewind` and 3 `!yes`: 30 s of voting, 30 s of veto countdown, then the
   desktop drops for a second and comes back as it was.
4. `!reboot` and 2 `!yes`: the desktop restarts (a minute or so). If it
   says "didn't shut down within 30 s", note it: the target's image needs a
   look.
5. `!reset` and 3 `!yes`: a forced restart, a few seconds.
6. On Discord, the panel's *Yes* and *No* light up during a vote and the
   count follows within 2 seconds; *Veto* lights up during a countdown.
7. Push *Democracy* (or `!democracy`) from 15 settled people, or `admin
   mode democracy`: commands become ballots, the strip shows the tally.
8. Someone who joined a minute ago tries `!yes`: refused, privately on
   Discord, on the overlay on YouTube.

Tell me what happened at each step (and anything that looked wrong on
stream); Phase 6 is done when 3, 4 and 5 work.
