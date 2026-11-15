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
| 3 | brain container (image, deploy), overlay websocket: the command log and grid on stream | next |
| 4 | YouTube and Discord adapters (needs your API credentials) | later |

## Step 2: drive the desktop by hand

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
