# Phase 3 — Broadcast pipeline (runbook)

Builds and runs **bcast**, the container that turns the target into a 1080p
YouTube stream: the target's desktop at 1:1 inside the stream layout, its
sound in sync, encoded for YouTube and relayed to it.

**Done when** (from PLAN.md): a 6-hour unlisted stream runs with no dropped
frames while the target is under full CPU load. Steps 1–6 get you a checked
pipeline without touching YouTube; steps 7–8 are the YouTube test streams.

## What you get

Image alias **`chat-uses/bcast`** (plus a dated alias per build), run as the
container **`bcast`** in project `infra` at `BCAST_IP` (10.20.0.10):

| Service | What it does |
|---|---|
| `chat-bcast-xvfb` | Virtual display `:99`, 1920×1080 |
| `chat-bcast-audio` | PulseAudio: sink `mix` (everything on stream) and sink `target` on top of it (the target's sound, mutable on its own) |
| `chat-bcast-websockify` | Serves the layout page and noVNC on `127.0.0.1:6080`, and relays noVNC's websocket to the target's VNC |
| `chat-bcast-browser` | Chromium in kiosk mode on `:99`, showing the layout page |
| `chat-bcast-audio-in` | Pulls the target's raw PCM (port 4712) into sink `target`, never more than ~0.1 s late |
| `chat-bcast-encoder` | x11grab + `mix` → x264 1080p30 CBR 7 Mbps, keyframe every 2 s, AAC 128k through a limiter → RTMP `live` on MediaMTX. Restarts itself if it stops producing frames |
| `chat-bcast-mediamtx` | MediaMTX v1.21.3: RTMP and RTSP on loopback only, HLS preview on port **8888** |
| `chat-bcast-forwarder` | Copies `live` to YouTube (RTMPS) without re-encoding; idle until a stream key or `FORWARD_URL` is set |

They all belong to `chat-bcast.target` and restart on their own. If one dies,
the others keep going: if the target crashes, the stream shows "Reconnecting
to the machine…" and recovers when it's back; if the YouTube connection
drops, only the forwarder restarts.

The **layout page** is `overlay/` in this repo (served by bcast itself, so the
stream doesn't depend on brain): the desktop 1600×900 at 1:1, click-grid
labels outside it (A–P below, 1–9 to the right), a sidebar with placeholders
for run, lives, mode, vote and saves, a strip for the command log and a
ticker, a clock, and a "Controls offline" badge until brain's websocket
(Phase 4) answers.

**The cursor** is drawn by the target's VNC server into the picture: the page
tells noVNC not to ask for a separate cursor, which a screen capture wouldn't
show, and not to ask for lossy JPEG/H.264, so text reaches the encoder pixel
for pixel.

**Scenes** cover the whole frame: `starting` ("Starting soon") and `brb` ("We'll
be right back", also the panic screen). Both mute the target's sound. Switch
from the host:

```bash
sudo incus exec bcast --project infra -- chat-bcast-scene brb "Back in five" "chat broke the network again"
sudo incus exec bcast --project infra -- chat-bcast-scene live
```

The page picks it up within a second. Brain will call the same command
(Phase 4, panic in Phase 5).

**Settings** are in `images/bcast/rootfs/etc/chat-uses/bcast.env.tmpl`
(filled in from `host.env` at build). Override any of them without rebuilding
in **`/etc/chat-uses/bcast.local.env` on the host**; `deploy.sh` pushes it.
Useful ones:

| Setting | Default | Notes |
|---|---|---|
| `TARGET_HOST` | `TARGET_IP` (10.30.0.10) | The target on stream |
| `VIDEO_BITRATE` | `7000k` | YouTube 1080p30: 4–10 Mbps |
| `X264_PRESET` | `veryfast` | `faster`/`fast` look better and cost more CPU |
| `AUDIO_LIMIT` | `0.8` | Limiter ceiling on the whole mix |
| `AUDIO_DELAY` | `0ms` | Shifts the sound against the picture; `check.sh` tells you how much |
| `FORWARD_URL` | empty | Send the stream here instead of YouTube (dry run) |
| `CHROMIUM_FLAGS` | empty | e.g. `--no-sandbox` if Chromium's sandbox can't start |

## Steps

### 1. Update the host

```bash
cd /opt/chat-uses && git pull
sudo infra/host/bootstrap.sh packages profiles
```

`host.env` gains two settings with defaults: `TARGET_IP` (the target on
stream, 10.30.0.10) and `BRAIN_PORT` (8080). Nothing to do unless you want
other values.

### 2. Rebuild the target image

Phase 3 changed how the target serves its sound (20 ms chunks instead of
300 ms bursts, for A/V sync), so rebuild it and keep a test target running:

```bash
sudo images/build.sh debian-xfce
sudo images/debian-xfce/check.sh --keep
```

The test target is `phase2-check` at `10.30.0.251`. Until brain launches real
targets (Phase 4), point bcast at it:

```bash
echo 'TARGET_HOST=10.30.0.251' | sudo tee /etc/chat-uses/bcast.local.env
```

### 3. Build bcast

```bash
sudo images/build.sh bcast
```

Like the target image: a short-lived builder on `infranet` runs
`images/bcast/provision.sh` and publishes `chat-uses/bcast`. It downloads
MediaMTX from GitHub and refuses it unless its SHA-256 matches the one pinned
in `provision.sh`. Expect 5–10 minutes.

### 4. Deploy

```bash
sudo images/bcast/deploy.sh
```

It launches `bcast` at `BCAST_IP` with the `bcast` profile (pinned CPUs 0–3),
pushes `bcast.local.env` and, if present, the stream key, and restarts the
pipeline. Later runs only push the config and restart; `--replace` recreates
the container from the latest image (the stream goes down for about a minute).

### 5. Check it

```bash
sudo images/bcast/check.sh --frame /root/stream.png
```

It reads the stream inside bcast the way the forwarder does and checks:

- the eight services are active and the layout page is served;
- the stream is H.264 1920×1080 at 30 fps plus AAC 48 kHz stereo;
- the encoder keeps real time (speed ≥ 0.97, 270+ frames in 12 s), and how
  much CPU bcast uses;
- a frame of the stream shows the desktop (saved as a PNG: look at it);
- a tone played in the target is heard in the stream, **in sync with the
  picture**. For that it opens a full-screen terminal on the target for a
  few seconds, which flashes as the tone starts, so run it before going live.
  The sync must be within what viewers accept (ITU-R BT.1359: sound at most
  90 ms early or 185 ms late). If it fails, it tells you how much to change
  `AUDIO_DELAY`.

### 6. Watch the preview

With the WireGuard tunnel up, open **`http://10.20.0.10:8888/live/`** in a
browser (HLS, a few seconds behind). Things to look at: text sharpness in a
terminal, the cursor, the grid labels, the sound. To see what scrolling text
costs, run something busy on the target (for example a full-screen terminal
running `base64 /dev/urandom`) and watch `journalctl -u chat-bcast-encoder`
inside bcast: its line every 30 s should keep `speed` at 1x and `dropped` steady.

### 7. Dry-run the forwarder

Send the stream back into MediaMTX instead of YouTube:

```bash
echo 'FORWARD_URL=rtmp://127.0.0.1:1935/dryrun/test' | sudo tee -a /etc/chat-uses/bcast.local.env
sudo images/bcast/deploy.sh
sudo incus exec bcast --project infra -- ffprobe -v error -show_entries stream=codec_name,width,height -of compact rtmp://127.0.0.1:1935/dryrun/test
```

You should see `h264` 1920×1080 and `aac`. Remove the `FORWARD_URL` line and
deploy again afterwards.

### 8. YouTube test streams

1. In YouTube Studio, create a stream with **unlisted** visibility and
   **ultra-low latency**; copy its stream key.
2. On the host:
   ```bash
   sudo install -d -m 0700 /etc/chat-uses/secrets
   sudo sh -c 'umask 077; cat >/etc/chat-uses/secrets/youtube-stream-key'   # paste the key, Enter, Ctrl-D
   sudo images/bcast/deploy.sh
   ```
   The key goes into bcast only (`root:bcast 0640`), never into an image.
   Errors from the forwarder may print the ingest URL, key included, to the
   journal inside bcast: don't paste those logs anywhere public.
3. Stream **1 hour**, then **6 hours** with the target under full CPU load:
   in a terminal on the target, `for i in 1 2 3; do yes >/dev/null & done`
   (one per target CPU). Record from the encoder's journal:
   speed, dropped frames; from the host: `incus info bcast --project infra`
   CPU; from YouTube Studio: stream health, and the delay between doing
   something on the target and seeing it on YouTube.
4. Put the numbers in PLAN.md section 15, decide bitrate and preset there, and
   tick Phase 3.

To stop sending to YouTube, delete the key file on the host and run
`deploy.sh` again (it removes bcast's copy).

## Operating it

- **Logs:** `sudo incus exec bcast --project infra -- journalctl -u 'chat-bcast-*' -f`.
  The encoder logs its progress every 30 s; the sound relay logs each time it
  had to drop sound to catch up.
- **Restart everything:** `sudo incus exec bcast --project infra -- systemctl restart chat-bcast.target`.
  Stop a single service the same way; it stays stopped until you start it again.
- **Stream down:** check `chat-bcast-forwarder` (YouTube side), then the
  encoder (`speed`, "stalled" lines), then MediaMTX, then the server's network.
- **Rebuild and roll back** like the target image (`docs/phase2.md`): rebuild
  with `images/build.sh bcast`, apply with `deploy.sh --replace`, older builds
  keep dated aliases.

## How it's wired

```
target (chatnet)                       bcast (infranet)
 Xtigervnc :5900 ──── VNC ──────────► websockify :6080 ──► noVNC ┐
                                      (also serves overlay/)     ├─ Chromium kiosk on Xvfb :99
                                      brain ws (Phase 4) ────────┘    │
 parec ── socat :4712 ── raw PCM ───► audio-in ── sink "target" ──┐   │ x11grab
                                      layout sounds ── sink "mix" ◄┘   │
                                                          │ monitor    │
                                                          ▼            ▼
                                      encoder (x264 + limiter + AAC) ── RTMP ──► MediaMTX "live"
                                                                                  ├─► forwarder ──► YouTube (RTMPS)
                                                                                  └─► HLS :8888 (WireGuard preview)
```

## What was tested before this landed

In the Debian 13 simulation (Phase 1's simulated server, Incus 7.6 from
Zabbly, the bcast container limited to 2 CPUs): both images were rebuilt from
scratch, the Phase 2 check passed on the new target image (except running
snapshots, which this sandbox can't do), and `check.sh` passed 7/7 against a
fresh bcast. Results:

- stream format exactly as YouTube recommends for 1080p30, encoder at 1x;
- CPU for the whole pipeline: **0.56 vCPUs** on a still desktop; **1.53 vCPUs**
  with a full-screen terminal scrolling random text (the worst case for VNC
  and x264), still at 1x with no frames dropped and text legible in the
  keyframe;
- **A/V sync −52 to +36 ms** on the path to YouTube, measured after a full
  restart, after encoder restarts, in a fresh container and on the forwarder's
  output (viewers notice from 45 ms early or 125 ms late);
- target crash: the stream kept running with the "Reconnecting" cover, the
  picture came back by itself and the sound 4 s after the target restarted;
- sound relay or player frozen for 2 s: dropped the backlog and was back in
  sync (+11 to +16 ms) instead of 2 s late for the rest of the stream;
- encoder frozen: the watchdog restarted it after 12 s;
- BRB scene: full-frame cover with custom text, the target's tone gone from
  the stream (−inf dB), still muted after a PulseAudio restart, and back at
  −10 dB on `live`;
- forwarder dry run: H.264 + AAC out of the forwarder, sound included.

Bugs found and fixed on the way (PLAN.md D23–D29):

- **The forwarder silently dropped all sound.** Over RTSP, MediaMTX's AAC
  packets carry no keyframe flag, and ffmpeg's stream copy waits for one.
- **Sound up to 1.5 s off the picture**, from five separate causes:
  PulseAudio's default 2 s buffer in audio-in; the target sending its sound in
  300 ms bursts; MediaMTX re-timing RTSP tracks by packet arrival (the pipeline
  now uses RTMP, one clock for both tracks); any stall leaving a backlog that
  was never caught up (audio-in now drops instead of queueing); and ffmpeg
  starting each capture input at zero on its own, which froze their start-up
  gap into an offset that changed with every encoder start (660 ms after one
  boot; the encoder now keeps their wall-clock timestamps).
- **The encoder hung forever when PulseAudio restarted** (ffmpeg kept waiting
  for sound): now a watchdog restarts it. Its speed in the journal is now
  measured over the last 30 s; ffmpeg's own figure averages the whole run.
- **BRB covered the picture but not the sound**: the target now plays into its
  own sink, which scenes mute; the mute is re-applied after PulseAudio restarts.
- A Chromium profile lock left by a crash could keep the browser from starting;
  MediaMTX enables Media-over-QUIC by default (turned off); `deploy.sh` left a
  removed stream key inside bcast.

**Not tested there** (no systemd in this sandbox's containers, and no
YouTube), so watch these on the server:

- the systemd wiring: unit ordering, `Restart=`, `RuntimeDirectory=`,
  `PartOf=chat-bcast.target`, the journal;
- Chromium's own sandbox inside an unprivileged container with AppArmor (if
  the browser keeps restarting, set `CHROMIUM_FLAGS=--no-sandbox`);
- real CPU numbers on the server's cores, with the target under load;
- anything YouTube: RTMPS ingest, stream health, its re-encode of text, and
  glass-to-glass latency;
- the HLS preview from a laptop over WireGuard.
