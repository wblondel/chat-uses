# Phase 7 — Closed beta → soft launch

PLAN.md Phase 7: an unlisted beta for Discord members (2–3 sessions of 2–3
hours, screen OCR in shadow mode), then screen OCR enforcing, then public
sessions on a schedule with a moderator online. Done when 5 public sessions
in a row have had no moderation incident that reached viewers.

Before it: Phase 5's gate (a second moderator's practice, the word lists
reviewed, the rules and privacy notice filled in) and Phase 6's stream test
with friends (`docs/phase6.md`, step 5).

Three steps of tooling, then the sessions themselves:

1. **Session reports** (below): what each session was like, in numbers.
2. **Screen OCR verdicts** (below): moderators mark each hit real or a false
   alarm, so the beta says when enforcing is safe.
3. **The beta runbook** (below): before, during and after a session; what
   to tune; the retro note.

## Step 1: session reports

PLAN.md asks, per session: viewers (peak and average), active chatters,
commands a minute, votes proposed and passed, panics, content holds (true
and false positives), encoder speed, CPU. Brain sees the chat side; only
the host sees bcast's encoder and every container's CPU (brain can't reach
bcast, D37). One command on the host does both:

```bash
sudo infra/host/session.py start "Beta 1"   # at the start of the stream
sudo infra/host/session.py status
sudo infra/host/session.py end              # when it's over: the report
```

**While it runs,** brain notes every minute how many are active in chat (a
message in the last 5 minutes), per platform. Not YouTube's concurrent
viewers any more (D75): YouTube's rules keep a statistic 30 days only, and
YouTube Studio has them. A brain
restart during a session doesn't end it; the minutes brain was down just
have no samples.

**At the end,** brain adds up from its store, for the session's window:

- viewers, peak and average (none when YouTube hides the count, no video is
  followed, or the quota is used up);
- people active in chat, peak and average, and each platform's peak;
- the people who sent commands, by platform;
- the commands: how many, how many a minute, accepted, and the refusals by
  reason;
- the votes: proposed, carried out (by kind), failed, vetoed, cancelled;
- panics, content holds (confirmed as panics, or resumed as false alarms),
  how long the stream was covered, screen OCR hits;
- time travel, reboots and resets.

It keeps the report and posts it in `#ops`. `session.py` then adds:

- **the encoder**: its average and slowest speed over the session, how
  many of its 30-second reports were below 0.97× (the stream falling
  behind), the frames dropped, any restarts (from bcast's journal);
- **CPU**: how busy the host was, and the CPUs' worth bcast, brain and the
  target used on average (from Incus).

It prints the whole report as Markdown and saves it in
`/var/lib/chat-uses/sessions/` (e.g. `20261120-1900-beta-1.md`), ready to
paste into the session's retro note. For example:

```markdown
## Session 'Beta 1': 2 h 05 min (2026-11-20, 19:00 UTC to 21:05 UTC)

- Viewers (YouTube): peak 23, average 14.2
- Active in chat: peak 12, average 7.5 (Discord 4, YouTube 9 at their peaks)
- People who sent commands: 31 (Discord 9, YouTube 22)
- Commands: 1834 (14.7 a minute): 1502 accepted, 332 refused (rate_limited 210, invalid 98, filtered 24)
- Votes: 9 proposed, 5 carried out (reboot 1 of 4, rewind 2 of 3, save 2 of 2); 3 failed, 1 vetoed
- Panics: 0; content holds: 0 (0 confirmed as panics, 0 false alarms resumed); covered 0.0 min in all
- Screen OCR hits: 3 (1 real, 2 false alarms, 0 not marked)
- Time travel: 2, reboots and resets: 1
- Encoder: average speed 1.0x, slowest 0.985x (never behind), 3 frames dropped
- CPU on average: host 41.2 % busy, bcast 1.6 CPUs, brain 0.1 CPUs, chatpc 0.9 CPUs
```

From brain alone (no encoder or CPU lines): `chat-brain-admin session start
NAME`, `session end`, and `sessions` for the last ten.

**Privacy:** a session's report holds counts only, never who. It's kept
after the 90 days that commands and logs are (`docs/privacy.md`).

### What was tested

- `control/tests/test_sessions.py`: a session started, sampled, carried
  over a brain restart and ended; a second one refused while one runs; the
  report's numbers from samples and the store (viewers, active people by
  platform, commands and refusals, votes by kind and result, panics, holds
  confirmed or resumed, time covered, OCR hits by mode) and its text; an
  empty session; through `chat-brain-admin`, with the report posted in
  `#ops`; the sampler waiting for a session.
- `control/tests/test_youtube.py`: viewers asked of YouTube only while
  streaming, a hidden count, no quota left.
- `infra/host/test_session.py` (in CI): the encoder's speeds and drops,
  across an encoder restart; CPU from Incus's counters and the host's
  `/proc/stat`; the Markdown; a whole session, start to end, against a fake
  `incus`.
- On the sandbox's Incus: the state API's `cpu.usage` (nanoseconds) that
  `session.py` reads; `journalctl --since @EPOCH`, which it uses for bcast's
  journal.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo infra/host/session.py start "Test"
# ... a few minutes, with someone sending commands ...
sudo infra/host/session.py end
```

The report should have the commands sent, the encoder's speed (about 1.0x)
and the CPU lines; `#ops` gets brain's part.

## Step 2: screen OCR verdicts

During the beta, screen OCR runs in shadow mode: a hit is only reported in
`#mod-alerts`, nothing happens on stream. Whether to let it hold the stream
(enforce) depends on how often it's right, so every hit now asks.

- **In `#mod-alerts`,** each hit's post (`👁️ Screen OCR hit #12: …`) has
  two buttons: **Real** and **False alarm**. Only moderators can press them
  (the Moderator or Owner role, or the server's owner); the post then says
  `Verdict: ✅ real (Mia)` or `Verdict: ❎ false alarm (Mia)`. The latest
  press counts, so someone else can correct it; the buttons work on old
  posts too, after a restart.
- **On the dashboard,** the log's `ocr_hit` lines have the same two buttons;
  the chosen one is outlined.
- **On the console:** `chat-brain-admin ocr-verdict 12 real` (or `false`).

`chat-brain-admin ocr` shows the record: the hits of the last 90 days, by
mode, how many were real, false alarms or not marked, and the last ten with
their verdicts. Session reports count them too (`Screen OCR hits: 3 (1 real,
2 false alarms, 0 not marked)`).

A content hold (enforce mode) already has a verdict of sorts: a moderator
who confirms it as a panic says it was real, one who resumes says it
wasn't. The session report counts those; a button's verdict on the hit
itself is the one `ocr` counts.

### When to turn enforcing on

Enforcing holds the stream until a moderator resumes, so each false alarm
covers the stream for as long as a moderator takes to look. A suggestion,
yours to decide (then record it in PLAN.md's decision log):

- every hit of the beta has a verdict (`ocr`: nothing "not marked");
- false alarms are rare: fewer than one per session;
- a moderator is online for every session (Phase 7's rule for public
  sessions anyway), and has practised resuming a hold.

Then set `OCR_MODE=enforce` in `/etc/chat-uses/brain.local.env` and run
`sudo images/brain/deploy.sh`. A false alarm that keeps coming back (a
word in a menu, say) is fixed in the word lists, not by turning enforcing
off.

### What was tested

- `control/tests/test_ocr_verdicts.py`: a hit posted with its buttons and
  its number; verdicts given, corrected (the latest counts), refused for a
  hit that doesn't exist or a word that isn't one; the record and the last
  hits through `chat-brain-admin`; a session report counting the verdicts
  of its own hits only; the buttons refusing people who aren't moderators
  and writing whose verdict it is on the post; the buttons' IDs carrying
  the hit; the dashboard showing and taking verdicts, refusing bad ones.
- A button press as Discord sends it (an `INTERACTION_CREATE` with the
  alert's message and its buttons), dispatched by discord.py's own code to
  the verdict and the edited post.
- The dashboard's log rendered in Chromium: the buttons on each hit, the
  chosen one outlined, the words still blurred.

### On the server

Deploy brain (`sudo images/build.sh brain && sudo images/brain/deploy.sh
--replace`). The next screen OCR hit in `#mod-alerts` has the buttons. To
see one without waiting, run red-team test 8 (`sudo infra/tests/redteam.sh
--only 8`, `docs/redteam.md`): it decodes a listed word on the desktop's
screen.

## Step 3: the beta, session by session

### Once, before the beta

- Phase 5's gate is done: a second moderator has practised (panic, resume,
  ban, lockdown, a content hold), the word lists have been reviewed, the
  rules and privacy notice are filled in and posted (`#rules`, the YouTube
  description).
- Phase 6's stream with friends went well (`docs/phase6.md`, step 5).
- Everything is deployed from the same commit, and every check passes:
  `images/brain/check.sh`, `images/bcast/check.sh`,
  `images/debian-xfce/check.sh --instance chatpc`, the red team
  (`infra/tests/redteam.sh`).
- The beta's schedule: 2–3 sessions of 2–3 hours, with the moderators who
  will be on. Post it in Discord's `#announcements` with the unlisted link
  shortly before each session (an unlisted video is open to anyone with the
  link: share it only there).
- Screen OCR stays in shadow mode (`OCR_MODE=shadow`, the default).

### Before each session (15 minutes)

```bash
sudo infra/host/golive.sh          # every check a script can do, a snapshot, then what's left by hand
                                   # (--drill: a panic drill too; --redteam: after a change)
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin status                       # YouTube: quota units left? Discord: panel shown?
admin youtube https://youtu.be/…   # the session's video, once it's live
sudo infra/host/session.py start "Beta 1"
```

When something goes wrong during the session: `docs/runbooks/`.

- A moderator is in `#mod-alerts` and has the dashboard open
  (`http://10.20.0.11:8080/admin` over WireGuard).
- Take a named save of the desktop as it starts (`!save start` with a
  friend's `!yes`, or `admin snapshot`): a known place to rewind to.

### During

- Moderators: answer every screen OCR hit (*Real* / *False alarm*), and use
  panic without hesitating; resuming costs nothing.
- Keep a note of what's fun and what isn't, as it happens: the retro is
  written from it.
- Laws can change live (`admin law NAME VALUE`, `admin law NAME default`;
  `admin laws` lists what's changed). Change one thing at a time, and say
  so in chat.

### After

```bash
sudo infra/host/session.py end     # the report: #ops gets brain's part, the whole is saved
admin youtube off                  # stop spending YouTube quota
admin ocr                          # hits still "not marked"? mark them now
```

Then the retro note: copy `docs/retros/TEMPLATE.md`, paste the report,
write what broke and what to change. A weekly one is enough once sessions
are routine.

### What to tune, by symptom

| What you see | The knob |
|---|---|
| Votes never pass, or pass with three people | the tier's quorum (`tiers.ACTION.quorum_fraction`, `quorum_floor`) and threshold, in the ruleset or `admin law` |
| Votes are over before people notice | `tiers.ACTION.window_s`; the veto countdown `veto_s` |
| The same vote again and again | `tiers.ACTION.cooldown_min` |
| Democracy flips back and forth, or never comes | `meter_step`, `meter_democracy_at`, `meter_anarchy_at`, `mode_lock_s`, `meter_drift_s` |
| Democracy feels slow | `democracy_window_s` |
| Many commands refused as `rate_limited` | the rate limits (one command every 2 s a person with a burst of 3, 8 a second in all) are code (`control/src/chatuses/ratelimit.py`): a change and a deploy |
| Innocent words refused (`filtered`) | the word lists (`control/src/chatuses/safety/lists/`, `docs/phase5.md`) |
| A screen OCR false alarm keeps coming back | the word lists too (OCR reads the screen through the same lists) |
| A raid | `/lockdown on` (regulars and people known for a day still play), bans |
| The stream falls behind (encoder below 0.97×) | the session report's CPU lines; `X264_PRESET`, `VIDEO_BITRATE` (`docs/phase3.md`) |

Every law has bounds (`chat-brain-admin laws`, `rulesets/`); a value out of
bounds is refused.

### Soft launch

When the beta's retros say it's ready (and enforcing is on, or decided
against): public sessions on a schedule, for example three evenings a week
for 3–4 hours, with you hosting and a moderator online. Same checklists;
the link goes public. Phase 7 is done after 5 public sessions in a row
with no moderation incident that reached viewers; then decide on 24/7 and
record it in PLAN.md.
