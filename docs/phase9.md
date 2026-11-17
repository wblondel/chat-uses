# Phase 9 — Depth

PLAN.md Phase 9, in this order: macros, the constitution (amendments), the
driver's seat with impeachment, chaos modifiers with the points shop, and
clicking on text. Done when each has simulator coverage and was live-tested
in at least one session.

1. **Macros** (below).
2. **The constitution: `!amend`** (below).
3. **The driver's seat and `!impeach`** (below).
4. **Chaos modifiers and the points shop** (below).
5. **Clicking on text: `!click "text"`** (below).

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

## Step 4: chaos

PLAN.md 6.11, `control/src/chatuses/game/chaos.py`. A modifier changes how
commands reach the machine or what the stream shows, never the machine
itself, so each is undone the moment it ends and none can break a run:

| `!buy` | Chaos | What it does | Where | For | Points |
|---|---|---|---|---|---|
| `mirror` | Inverted mouse | clicks land mirrored across the screen | brain | 60 s | 80 |
| `azerty` | AZERTY keyboard | typed text comes out as on an AZERTY keyboard | brain | 60 s | 80 |
| `dvorak` | Dvorak keyboard | ... as on a Dvorak (`hello` types `d.nnr`) | brain | 60 s | 80 |
| `caps` | Caps Lock glued | letters come out in the other case | brain | 60 s | 40 |
| `drunk` | Drunk mouse | clicks land up to 20 px off | brain | 60 s | 60 |
| `lag` | Input lag | 5 s before each command runs | the executor | 60 s | 100 |
| `shuffle` | Shuffled grid | the grid's labels change places; chat's clicks follow them | overlay and parser | 60 s | 80 |
| `flip` | Upside down | the stream shows the screen upside down | overlay | 30 s | 60 |
| `quake` | Earthquake | the stream shakes | overlay | 15 s | 30 |
| `blackout` | Blackout | the stream shows nothing: chat plays blind | overlay | 30 s | 80 |

**One at a time**, and 5 minutes from one's end to the next one's start.
Three ways in:

- **`!buy NAME`** spends its price in points (`/me` tells you yours);
  refused with the reason (`Earthquake costs 30 points: you have 10`,
  `Inverted mouse is on: 42 s left`, `chaos again in 5 min`), and nothing
  spent on a refusal. Points still never go down any other way.
- **Chance:** every 20 minutes, one chance in two of a random one, while
  the machine is alive and nothing's covering the stream.
- **A Super Chat** on YouTube: its tier picks one (1 Earthquake, 2 Upside
  down, 3 Caps Lock, 4 Blackout, 5 Shuffled grid, 6 and up Inverted
  mouse). Paid triggers buy chaos only, never a vote (PLAN.md 6.11). They
  skip the cooldown and wait their turn (10 at most). brain reads only the
  tier: never the amount, nor the Super Chat's message, which isn't a
  command.

**The filter still decides what's typed.** Keyboard modifiers change a
command when brain accepts it, and the text it would really type goes
through the filter again: text aimed to become a slur once remapped is
refused (`the chaos turned that into something the filter refuses`). The
overlay's log shows the command as chat sent it.

The overlay shows a Chaos panel (what, how long, from whom or by chance),
the stream's own effects (upside down, shaking, black), and the shuffled
labels; chat clicks by the labels it sees. A panic stops chaos at once, as
does a ruleset without `chaos` in its features (then nothing starts).

Moderators: `chat-brain-admin chaos` (what's on, the shop), `chaos start
NAME` (free, now), `chaos stop`. The site's Laws page lists the shop.

### What was tested

- `tests/test_chaos.py`: ten modifiers, none in the target; Dvorak, AZERTY
  and Caps Lock on typed text; mirrored and drunk clicks, moves and drags;
  a macro changed step by step, the others leaving commands alone; the lag;
  a shuffled grid that chat aims with, and the real one back after; one at a
  time and the cooldown; Super Chats queued without a cooldown, 10 at most;
  chance only when brain allows it; points bought and spent, nothing spent
  on a refusal. Through brain: a purchase, the overlay's chaos message and
  the status line, a mirrored click queued while the log shows chat's, one
  at a time, a panic stopping it; a Dvorak remap refused when the filter
  refuses what it would type; a shuffled grid's click landing on the real
  cell; the admin's start and stop; chaos off in a ruleset; a Super Chat's
  tier from the YouTube adapter (its message not delivered as a command)
  starting chaos, the next one queued.
- The simulator's `chaos` scenario: too few points, enough; one at a time;
  the cooldown; a mirrored click; a shuffled grid's click; Super Chats one
  after another.
- The overlay in Chromium from a fake feed: upside down (the screen turned
  180°), blackout, the shuffled labels and the Chaos panel, all gone when
  it ends.

### On the server

Brain and bcast: `sudo images/build.sh brain && sudo images/brain/deploy.sh
--replace`, then `sudo images/build.sh bcast && sudo images/bcast/deploy.sh
--replace` (between streams). `admin chaos start quake` shakes the stream
for 15 s; `admin chaos` shows the shop.

## Step 5: clicking on text

PLAN.md 6.1, `control/src/chatuses/textclick.py`. Chat clicks on what it
reads instead of a cell:

| Command | Does |
|---|---|
| `!click "Settings"` | clicks the middle of the first "Settings" on the screen |
| `!click "Settings"#2` | the second one (`#1` to `#9`, in reading order: top to bottom, then left to right) |
| `!dclick` `!rclick` `!mclick "Trash"` | double, right and middle clicks, the same way |

The text is 1 to 40 characters, one or more words on one line; case and the
punctuation around words don't matter (`"file system"` finds `File
System…`). Straight or curly quotes both work (phones type curly ones). A
macro can't hold one: a macro aims at cells, and what's on the screen
changes.

**Found when its turn comes**, not when chat sent it: brain takes a frame
from the target's screen (the read-only VNC grabber, as screen OCR), reads
it with tesseract twice, as it is and in black and white (a 1-bit image:
small dark text on grey buttons is missed in the first and read in the
second), and merges the words. Each reading takes well under a second on
a desktop; the two run at once, 10 s at most each, 15 s for the lot.

**Not there, nothing is clicked.** The overlay's log line says `not on the
screen`; the same if the screen can't be read (tesseract failed, VNC
down). A click that can't be aimed is a miss, never a guess.

What the screen says is the target's, and hostile: it decides only where a
click lands, never what is shown or typed, and none of it is logged. The
worst a lying screen can do is a misclick. Chat's quoted text goes through
the text filter (it's on the overlay). Chaos's mouse works on these too:
mirrored or drunk, the click found is moved as any click is.

The executor records the click it became (its cell and point), so the audit
viewer shows where it landed; the `commands` table keeps what chat sent.

Not built: PLAN.md's optional accessibility (AT-SPI) helper inside the
target. OCR finds the labels of an ordinary desktop; the helper can come
later if OCR misses too much in live play.

### What was tested

- `tests/test_textclick.py`, on tesseract's real output for a 1600x900
  desktop drawn in Chromium (top bar, desktop icons, a settings dialog,
  `tests/fixtures/`): every label found within 8 px of its true middle but
  one (the second Apply, bottom right, which neither reading sees);
  Cancel and Apply found only in the black-and-white reading; both
  "Settings" once each, in reading order; `#n`; case and punctuation; a
  phrase only on one line, in order; the 1-bit image byte for byte. With a
  stand-in tesseract: both readings run and merge, one that fails or hangs
  leaves the other, neither is an error. The grammar and its refusals
  (empty, `#0`, unclosed, too long, extra words, in a macro); described as
  typed and stored whole. The executor: resolved when its turn comes and
  clicked there, recorded as the click; a miss sends nothing; a screen too
  slow to read is a miss and the queue moves on. Through brain: grid
  coordinates from a screen twice the size, `#n` past the last match, a
  mirrored text click, a screen that can't be read, the filter refusing
  the quoted text, and the overlay's line saying it missed.
- The simulator: text clicks in chat's mix, hostile ones refused (too
  long, unclosed, `#0`, bidi characters, in a macro).
- By hand, the same desktop through the real tesseract: every label above
  found, 0.2 s a reading.
- The overlay in Chromium from a fake feed: a text click done, and one
  missed, struck through with `not on the screen` beside it.

### On the server

Brain only: `sudo images/build.sh brain && sudo images/brain/deploy.sh
--replace` (tesseract is already in its image, for screen OCR). In
`#control` or on stream: `!click "Applications"` opens the menu;
`!click "nothing here"` says it isn't on the screen. The overlay's new
"missed" wording needs bcast's layout page: `sudo images/build.sh bcast &&
sudo images/bcast/deploy.sh --replace` between streams (before it, a missed
click shows as dropped, without the reason).
