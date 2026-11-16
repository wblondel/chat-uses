# Phase 6 — Governance and time travel

PLAN.md Phase 6: chat votes on power actions and time travel, switches
between anarchy and democracy, and rulesets set the numbers. Five steps,
each tested before the next:

1. **Rulesets, laws and power votes** (this page, below).
2. Snapshots and power: auto snapshots, named saves, `!rewind`, `!load`,
   `!reboot`, `!reset`, the overlay's timeline.
3. Democracy mode: the meter, 12-second ballots, tally bars.
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

**Until step 2,** a vote that passes says "isn't built yet": snapshots,
rewinds, reboots and resets are next.

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
says the vote passed and that the reboot isn't built yet. On your own, two
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
