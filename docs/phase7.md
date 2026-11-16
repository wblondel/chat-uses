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
2. Screen OCR verdicts: moderators mark each hit real or a false alarm, so
   the beta says when enforcing is safe.
3. The beta runbook: before, during and after a session; what to tune; the
   retro note.

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

**While it runs,** brain notes every minute how many people watch (YouTube's
concurrent viewers: 1 quota unit a minute, so 60 an hour) and how many are
active in chat (a message in the last 5 minutes), per platform. A brain
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
- Panics: 0; content holds: 0 (0 confirmed as panics, 0 resumed as false alarms); covered 0.0 min in all; screen OCR hits: 3
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
