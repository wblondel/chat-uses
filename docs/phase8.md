# Phase 8 — Roguelike layer and public site

PLAN.md Phase 8: runs with lives, deaths and new runs; achievements and
points; a public site; posts in Discord's `#graveyard` and `#achievements`.
Done when a full run lifecycle works end to end: start → achievements →
rewinds → death → Graveyard on the site and Discord.

1. **Runs, lives, deaths, new runs** (below).
2. Achievements and points.
3. The public site.
4. The end-to-end test.

## Step 1: runs, lives, deaths, new runs

`control/src/chatuses/game/runs.py`, PLAN.md 6.6 and 6.7.

**A run** is one life of the machine, numbered from 1. It starts with
`lives_start` lives (a law, 3). The sidebar shows its number, its lives
(♥♥♡) and how long it has lived. brain starts run #1 the first time it
runs; a restart keeps the run going.

**Lives.** Each `!rewind` and `!load` chat votes costs a life, and so does a
revive. With none left chat can't rewind (`no lives left: the next death
ends the run`), and the next death ends the run. Achievements will give
some back (step 2). A ruleset without `lives` in its features keeps deaths
and runs, and lives cost nothing.

**Death.** The watchdog says what would kill the machine
(`Watchdog.fatal`):

| What | Cause on the card |
|---|---|
| the target is stopped (`poweroff` from chat, say) | it was switched off |
| systemctl can't run in it (`rm -rf /`) | its system was destroyed |
| VNC down for 3 minutes, after the display's restart | its display died / its screen stopped answering |
| the screen hasn't changed in 2 minutes after input | it froze |

A watchdog that can't check the target is our problem, not the machine's:
never a death. A death is noticed once the cause has lasted a minute, never
while a moderator has the stream covered (panic, hold), nor while the
machine is travelling, restarting or between runs, nor in the 3 minutes
after a revive or a new run (it boots).

**The death vote.** For 60 seconds chat votes `!revive` or `!rip`, among
the people who were around when it died (as for power votes). Revive wins
ties and silence while a life is left: brain puts the machine back to the
last auto snapshot from before it fell ill (or forces a restart if there's
none), for a life. A power vote running when it died is cancelled, and
none can start until it's decided. With no lives left there's no vote.

**The end.** A dead run, or one chat abandons with `!newrun` (tier 4: 75 %,
a quorum of 35 % and at least 4, a 60-second veto countdown), gets its card:
on stream for 20 seconds, full-frame, and in `#graveyard` with its last
screen. The card says how it died, how long it lived, the lives it used,
its last commands, and the final blow, "probably killed by" whoever sent
the last command that ran in the minute before the machine was last seen
well (a joke, not an accusation). Commands are written as the overlay
writes them, never as chat typed them (D32), and names are the overlay's
(filtered).

**The next run.** While the card shows, brain deletes the dead run's
snapshots (their thumbnails stay, for the site) and puts the machine back
to its **golden snapshot**, the desktop as deployed: `deploy.sh` takes it
once a new target's desktop answers. Then `RUN #N BEGINS` for 6 seconds,
lives back to `lives_start`, input open again. Without a golden snapshot
the new run carries on with the same desktop, and `#ops` says so.

Moderators: `chat-brain-admin run` (the run, its lives, how long), `run
end [CAUSE]` (end it now: its card, then a new run). Moderators' own tools
(`/rewind-to`, panic) cost no lives and work during a death vote.

`#ops` hears every death, revive and new run.

### What was tested

- `tests/test_runs.py` (fake clock): the first run, lives spent and given
  back, lives off; a death only after a minute of illness, not while quiet
  or booting, the count starting again after a good check; the death vote,
  revive winning ties and silence, rip winning a majority, a changed
  ballot, no vote without lives; the card's last commands (not votes) and
  final blow; the next run's number and lives.
- `tests/test_run_lifecycle.py` (brain on a fake Incus): a death cancels the
  power vote, `!revive` and `!rip` from chat, proposals refused while dying,
  a tie revives to the last snapshot for a life; `!rip` ends the run with
  its card on stream and in `#graveyard`, then the golden snapshot, the old
  snapshots gone, `RUN #2 BEGINS`, input open; no lives, no vote and no
  rewinds; no golden snapshot, `#ops` told; `!newrun` voted through its veto
  countdown; `chat-brain-admin run` and `run end`.
- `tests/test_watchdog.py`: what's fatal and what isn't (a check that can't
  run).
- The overlay rendered in Chromium from a fake feed: the run panel, the
  death vote (with and without lives), the death card and the new run's.
- On the sandbox's Incus: `incus rebuild` refuses a container that has
  snapshots (so it can't give runs a fresh start without deleting panic
  evidence), hence the golden snapshot; `deploy.sh`'s check for it.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/debian-xfce/deploy.sh            # says if chatpc has no golden snapshot
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin run                                     # Run #1: 3 of 3 lives left, …
```

If `deploy.sh` says chatpc has no golden snapshot (it was deployed before
this), `sudo images/debian-xfce/deploy.sh --replace` gives it a fresh
desktop and one. To see a whole death without breaking anything:
`admin run end testing` shows the card, posts in `#graveyard` and starts
run #2 from the golden snapshot.
