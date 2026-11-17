# Phase 4 — Control plane MVP (runbook)

Builds **brain**: chat messages in, mouse and keyboard on the target out.
The code is in `control/` (developer notes in `control/README.md`); this file
is what to do on the server. Phase 4 lands in steps, and this runbook grows
with them.

**Done when** (from PLAN.md): on an unlisted stream, you and a friend control
the desktop from both YouTube and Discord, and the command-to-visible latency
is measured and written down.

| Step | What | State |
|---|---|---|
| 1 | Parser, grid, keymap, rate limits, identity, store, chat simulator, CI | done |
| 2 | VNC injector and anarchy executor; `drive` tool to try them by hand | done |
| 3 | brain container (image, deploy, check), overlay feed: the command log, grid and pointer names on stream; console chat | done |
| 4 | YouTube and Discord chat adapters; `chat-brain-admin` | done; to try on the server with your credentials |

## Step 2: drive the desktop by hand

(Once brain is deployed, step 3's `chat-brain-say` does the same through brain
and shows on the overlay; this stays useful for poking at a desktop directly.)

Nothing runs on its own yet: `chatuses.drive` sends chat commands you type on
the command line, through the real parser and injector, to a desktop's VNC
port.

**From a container, never from the host:** the target is hostile (PLAN.md
5.4), so whatever talks to its VNC server runs in an unprivileged container.
Until brain exists, use bcast. It already talks to the target's VNC for the
stream's picture, and the firewall lets it.

### 1. Update the repo and rebuild the target image

The desktop image now gives the session its UTF-8 locale. Without it, accented
letters typed by chat (é, à, ç) are dropped by the terminal's shell.

```bash
cd /opt/chat-uses && git pull
sudo images/build.sh debian-xfce
sudo images/debian-xfce/check.sh --screenshot /root/desktop.png
sudo images/debian-xfce/deploy.sh --replace
```

The check tests the new image on its own throwaway target and should end with
every line passing, including `session locale is en_US.UTF-8`. `deploy.sh
--replace` then gives the target on stream, `chatpc`, a fresh desktop from it
(what chat did on the old one, and its snapshots, go). bcast reconnects on its
own; if the stream is live, you'll see the desktop come back.

### 2. Drive it

```bash
sudo incus file push -r control/src/chatuses bcast/opt/ --project infra
drive() { sudo incus exec bcast --project infra -- env PYTHONPATH=/opt python3 -m chatuses.drive 10.30.0.251 "$@"; }

drive '!key ctrl+alt+t' '!wait 3' '!type echo Chat était là' '!enter'
drive '!rclick H5' '!wait 2' '!esc'
drive '!dclick A1'
```

Each command prints how long it took to send (a combo takes about 8 ms, a
40-character `!type` about 0.3 s: keys go out 8 ms apart so applications keep
up). Commands use the same grammar as chat (PLAN.md 6.1), including its limits,
and `!wait N` pauses N seconds (here only). Watch the result in a VNC viewer
over WireGuard (`10.30.0.251:5900`) or on the stream preview.

To measure command-to-visible latency on the stream, send a click that changes
something big (`!rclick H5` opens the desktop menu in the middle of the
screen), and time the gap between pressing Enter and the change on YouTube.
Subtract nothing: that's what chat will feel.

### 3. Clean up

```bash
sudo incus exec bcast --project infra -- rm -r /opt/chatuses
```

The next bcast deploy replaces the container anyway.

## Step 3: brain on the server

brain runs in its own container (`brain`, project `infra`, 10.20.0.11): the
pipeline, the injector, and the overlay feed that bcast's layout page shows.
Until the chat adapters arrive (step 4), you talk to it through **console
chat**: `chat-brain-say` inside the container sends messages exactly as if a
viewer had typed them (same parser, rate limits and log on stream).

Do this after the 6-hour run: step 2 below replaces bcast, so the stream
drops for about a minute.

### 1. Update the repo

```bash
cd /opt/chat-uses && git pull
```

### 2. Rebuild bcast (new layout page)

The layout page now shows the command log, flashes the grid and names who
moved the pointer. Its browser also starts without a cache, so a new page is
never hidden behind an old copy.

```bash
sudo images/build.sh bcast
sudo images/bcast/deploy.sh --replace
sudo images/bcast/check.sh
```

Until brain runs, the corner badge says "Controls offline".

### 3. Build and deploy brain

brain drives the target on stream, `chatpc` at 10.30.0.10, the image's default
(`images/debian-xfce/deploy.sh` launches it):

```bash
sudo images/build.sh brain          # about 2 minutes
sudo images/brain/deploy.sh
sudo images/brain/check.sh
```

The check should pass 8 lines: the service, the health check, the VNC link to
the desktop, the overlay feed from the host and from bcast, bcast's page
pointing at it, and a console `!move H5` that moves the desktop's pointer and
shows up on the feed. The corner badge on stream turns to "Controls online".

### 4. Play

```bash
say() { sudo incus exec brain --project infra -- chat-brain-say --as "$@"; }

say william '!key ctrl+alt+t' '!type echo Chat était là' '!enter'
say alice '!rclick H5'
say bob '!grid'
say troll '!click Z99'          # refused: the log shows only brain's reason
```

`--as NAME` is the name shown on stream. Messages after the first wait 1 s
(`--pause`); rate limits apply as for any viewer, so one name can't send
more than a few commands in a row.

### 5. Measure the latency

Phase 4's "done when" asks for the command-to-visible latency. Watch the
stream on your phone, press Enter on `say alice '!rclick H5'` and time until
the menu appears on YouTube. Do it a few times and send me the numbers; I'll
record them in PLAN.md.

### Operating it

- Logs: `sudo incus exec brain --project infra -- journalctl -u chat-brain -f`.
- Health: `curl http://10.20.0.11:8080/healthz` (from the host or the
  WireGuard laptop): `"target": true` means brain's input reaches the desktop,
  `"overlay_pages": 1` means bcast's page is listening.
- A setting changed in `/etc/chat-uses/brain.local.env`: run
  `images/brain/deploy.sh` again (it pushes the file and restarts brain).
- Restarting brain doesn't touch the stream: the page shows "Controls
  offline" for a few seconds and reconnects on its own. The log on stream
  starts empty again (it lives in brain's memory; every command is also in
  the database).

## Step 4: YouTube and Discord chat

brain reads commands from YouTube live chat and from Discord's `#control`,
next to console chat. Viewers get no replies in chat: the overlay is the
answer (PLAN.md D8), with a red dot for YouTube and a blurple one for Discord.

Do steps 1–2 once. Before you start, do docs/phase0.md sections 5 (Google
Cloud: API key) and 6 (Discord: application, bot with the Message Content
intent, invited to your server, `setup_server.py` run).

### 1. Put the credentials on the server

```bash
sudo install -d -m 0700 /etc/chat-uses/secrets
sudo sh -c 'umask 077; cat >/etc/chat-uses/secrets/google-api-key'      # paste the API key, Enter, Ctrl-D
sudo sh -c 'umask 077; cat >/etc/chat-uses/secrets/discord-bot-token'   # paste the bot token, Enter, Ctrl-D
```

Then tell brain which Discord server it serves (Discord: Developer Mode on,
right-click the server icon → Copy Server ID):

```bash
sudo tee -a /etc/chat-uses/brain.local.env <<<'DISCORD_GUILD_ID=<SERVER_ID>'
cat /etc/chat-uses/brain.local.env    # DISCORD_GUILD_ID=...
```

Commands are read in `#control` only (`DISCORD_CHANNEL` to change it). The
secrets go into brain only (`root:brain 0640`), never into an image. Never
paste them in chat, including this one.

### 2. Rebuild and redeploy brain

```bash
cd /opt/chat-uses && git pull
sudo images/build.sh brain
sudo images/brain/deploy.sh --replace
sudo images/brain/check.sh
```

The check now ends with the chat adapters. Expect `Discord: connected,
reading #control`, and for YouTube "no video set" (next step). If Discord
says the token was refused or the Message Content intent is off, fix it in
the Developer Portal, then `sudo images/brain/deploy.sh` (it pushes the
token again and restarts brain).

### 3. Each stream: point brain at it

Start the unlisted stream as in Phase 3, copy its URL (YouTube Studio → the
stream → share, or the watch page), then:

```bash
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin youtube https://youtube.com/live/VIDEO_ID
admin status
```

`status` should say `YouTube: streaming, video VIDEO_ID` within a few
seconds. Before the stream is live it says `waiting` and checks once a minute.
brain keeps the video across restarts and rebuilds (its store is on the
`brain-data` volume, which `deploy.sh --replace` keeps: D53). When a stream ends, its chat ends
too: set the next stream's video (`admin youtube off` stops reading).

### 4. Play, and measure the latency

In YouTube chat type `!type hello`; in Discord `#control`, `!enter`. Both
appear in the overlay's log with their dot; `/status` in Discord answers only
you. Then with a friend, one on each platform, control the desktop together:
that's Phase 4's "done when".

For the latency, time from sending a chat message (`!rclick H5` is easy to
see) to the menu showing on the stream, a few times from each platform. Send
me the numbers and I'll write them down in PLAN.md.

### How the chat adapters behave

- **YouTube** finds the chat with `videos.list` (1 quota unit), then keeps a
  gRPC stream open (`liveChatMessages.streamList`, 5 units per connection);
  YouTube pushes messages as they're posted. After a disconnect it resumes
  where it stopped, so nothing is missed or read twice.
- **The quota** is 10,000 units a day, reset at midnight Pacific (08:00 or
  09:00 UTC). Every connection costs 5 units however long YouTube keeps it
  open, and YouTube closes them after about 10 s: reconnecting at once
  would spend the day's quota in about 5 hours (it did, on 17 November).
  So brain paces its connections (D73). The day's quota builds up, a
  little at midnight Pacific and evenly through the day to 9,000 by the
  next, and a new connection waits until it's covered. While chat is
  quiet (no message for 3 minutes) it also waits about a minute (66 s
  with the default quota, 90 s at most), so quiet hours leave quota for
  busy ones. With the default quota and the stream on all day, a busy
  chat waits between nothing (after quiet hours) and about 40 s (busy all
  day) before each reconnection. What's said meanwhile comes when it
  reconnects, late but not dropped. It never runs out before the reset.
- `admin status` shows the units spent today, the calls in the last hour,
  how long the closed ones lasted on average, how long the open one has
  been open, or how long until the next (`… quota units today (N calls in
  the last hour, 5 units each; closed ones lasted S s on average; next
  call in T s)`). brain keeps the day's count in its store, so a restart
  doesn't start it again, and stops at 9,000. If it's used up anyway (by
  that count, or because Google refuses: `quotaExceeded`), YouTube chat
  waits for the reset and goes on by itself; `#ops` is told once, and
  `admin status` says until when (D60). Stop following the chat when the
  stream is off (`admin youtube off`).
- **For chat without waits, ask Google for more quota**: the YouTube API
  Services audit and quota extension form (linked from Google Cloud
  console → APIs & Services → YouTube Data API v3 → Quotas). It's free
  and reviewed by Google, which can take a few weeks. With about 100,000
  units a day, brain reconnects at once all day by itself: nothing to
  change here.
- **Commands older than 30 s are dropped**, on both the first connection
  (YouTube sends recent history) and after an outage: a command executed
  minutes late would surprise everyone. A wait brain chose doesn't count:
  the 30 s start when it ends (90 s of it at most).
- **Discord** reconnects by itself. A refused token or a missing intent stop
  it with the reason in `admin status` and in the check.
- Roles come along with each message: YouTube owner, moderator, member and
  verified badges; Discord's Owner, Moderator and Regular roles. They're
  recorded now and used by Phase 5's moderator commands.

## How the overlay feed works

- One way: brain → page, a websocket at `ws://10.20.0.11:8080/ws/overlay`.
  The page sends nothing brain reads. Everything in the feed is already on
  stream, so it has no password; at most 16 pages, and a page that stops
  reading is let go.
- The log shows each command as brain understood it (`!TYPE  hi` shows as
  `!type hi`), never the raw message. Refusals show only brain's fixed
  wording, at most once per viewer every 10 s, so one spammer can't push
  everyone else off the log. That includes "the machine isn't taking input
  right now", while the desktop is down (the corner badge says so too) or
  16 commands are already waiting.
- Names are cleaned before they're drawn: invisible and control characters
  removed (bidi tricks, zero-width spaces), 24 characters at most. Whether a
  name may appear at all is Phase 5's filter.
- Console chat listens on a Unix socket inside brain, never on the network:
  only someone with a shell on the server can use it.

## How the injector behaves

- **One command at a time, in arrival order** (anarchy). At most 16 wait their
  turn; past that, new ones are refused, so commands never land long after
  chat sent them.
- **If the target goes away** (reboot, rewind, crash), commands still waiting
  are dropped, not replayed on the next desktop, and the injector reconnects
  on its own: after 1 s, then backing off to every 15 s.
- **Nothing stays held down.** If a command is cut short (lost connection,
  10 s timeout), the injector releases every key and mouse button it pressed.
- **Clicks follow the screen.** Grid coordinates are scaled to the size the
  VNC server reports, in case a target runs at another resolution.
- **The VNC server is hostile.** Every step has a timeout, every length it
  sends is capped, and after the handshake nothing it sends is parsed, only
  read and thrown away. The injector never asks for the screen.

## What was tested before this landed

Step 4:

- 305 unit tests, among them the YouTube adapter against a fake gRPC chat
  server built from Google's own `.proto`: chat streamed, resumed from the
  page token after a normal close and after errors, history and backlog
  older than 30 s dropped, duplicates dropped, Super Chats ignored, every
  documented error (chat ended, key refused, bad request, rate limit),
  `videos.list` answers (not live yet, no such video, ended, key refused,
  quota exceeded, a Google server error retried), the quota guard. The Discord adapter's message rules
  (server, channel, bots, roles) and its stops on a bad token or a missing
  intent. brain: adapters off without their secrets, the video kept across
  restarts, admin requests over the console socket, a database from step 3
  upgraded.
- In the sandbox: the brain image built with the new dependencies (all 14
  hash-checked), deployed, and checked 7/7 with both adapters reported off
  without secrets. With stand-in secrets: deploy pushed them as
  `root:brain 0640`, `chat-brain-admin` followed a video, refused a bad URL
  and kept the video across a restart, and status and the check showed why
  each adapter wasn't connected (the sandbox can't reach Google or Discord:
  "last error: ...DNSError", retried with backoff). The real connections
  are yours to try (steps 1–4 above).

Step 3:

- 256 unit tests, among them brain end to end against a fake VNC server
  (click → desktop events, log line, its ✓, the pointer name), refusals and
  their 10 s limit, a target that's down ("busy"), console chat's socket and
  bad requests, the feed's slow and too many pages, and brain stopping at
  once with a page connected.
- In the sandbox: the brain image built (dependencies hash-checked), deployed
  at 10.20.0.11, `images/brain/check.sh` passing. Console chat from six names
  drove the real desktop through bcast's real pipeline: a terminal opened and
  typed "chat était là" (accents and an emoji name on stream), `!rclick H5`
  opened the menu under dave's name with the grid flashing, `!grid` named
  every cell, the troll's refusal showed only the reason, and the frame read
  back from the encoded stream shows it all.
- With the desktop stopped, six commands from two viewers gave one line each
  ("the machine isn't taking input right now") and the badge "Machine not
  taking input"; when it came back, brain reconnected by itself. brain
  stopped 0.05 s after SIGTERM with the page connected, and the page was
  back on its own seconds after brain restarted.
- Found and fixed there: Chromium kept running a cached copy of the old
  layout page for a while after the files changed (bcast's browser now starts
  without a cache), and brain took 32 s to stop while a page was connected
  (it now closes the pages first: under a second).

Step 2:

- Unit tests against a fake VNC server: the exact bytes of every command, and
  14 hostile servers (wrong protocol, refusals, huge or endless lengths,
  silence, garbage after the handshake, hang-ups). Also run with every CPU busy.
- In the sandbox, from a container at brain's address through the real
  firewall, on the Phase 2 desktop: a terminal opened with `ctrl+alt+t`, a
  typed command wrote `Chat était là, ça va` into a file as user `chat`;
  capitals with accents, `ÿ` and `~|{}\` typed right; `!rclick H5` opened the
  desktop menu exactly at (750, 450); `!dclick A1` opened the Home folder. The
  missing locale was found here: before the fix, the accents were dropped.
- The target restarted under a running injector: offline noticed at once with
  no command in flight, 16 commands refused while it was down, back on its
  own 3 s after the desktop returned.
