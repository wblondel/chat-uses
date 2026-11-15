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
| 4 | YouTube and Discord adapters (needs your API credentials) | next |

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
sudo images/debian-xfce/check.sh --keep --screenshot /root/desktop.png
```

The check replaces `phase2-check` with a fresh desktop from the new image and
should end with every line passing, including `session locale is en_US.UTF-8`.
bcast reconnects to it on its own; if the stream is live, you'll see the
desktop come back.

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

Point brain at the test desktop first (the image default is the future real
target, 10.30.0.10):

```bash
echo 'TARGET_HOST=10.30.0.251' | sudo tee /etc/chat-uses/brain.local.env
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
