# Chat Uses — Project Plan

A live channel (YouTube for video, Discord as control room) where **chat controls a real computer**: types, clicks, reboots, rewinds, and votes on what happens next. Built on a single netcup KVM server (8 vCPU / 16 GB RAM / 320 GB SSD) **without nested virtualization**.

This document is the plan to build it. Follow it in order: each phase ends with a "Done when" checklist, and **no public stream happens before Phase 5 is complete**.

---

## Contents

1. [How to use this plan](#1-how-to-use-this-plan)
2. [Vision and pillars](#2-vision-and-pillars)
3. [Constraints and assumptions](#3-constraints-and-assumptions)
4. [Key decisions](#4-key-decisions)
5. [Architecture](#5-architecture)
6. [Game design spec](#6-game-design-spec)
7. [Safety and moderation spec](#7-safety-and-moderation-spec)
8. [Roadmap (phases)](#8-roadmap-phases)
9. [Show formats backlog](#9-show-formats-backlog)
10. [Operations](#10-operations)
11. [Legal and policy](#11-legal-and-policy)
12. [Risks](#12-risks)
13. [Open questions](#13-open-questions)
14. [References](#14-references)
15. [Decision log](#15-decision-log)

---

## 1. How to use this plan

- **Phases are gates.** Finish a phase's "Done when" list before starting the next. Phases 9 and 11 can be picked from in any order.
- **Checkboxes** (`- [ ]`) are the task list. Tick them in commits as work lands.
- **⚠️ Verify** marks a fact that was true when this was written but changes often (APIs, library support). Check it when you reach that task.
- **Sizes** are rough, for one person working evenings and weekends: **S** = a few evenings, **M** = 1–2 weekends, **L** = 3+ weekends.
- Record every changed decision in the [decision log](#15-decision-log) instead of silently editing section 4.

### Progress tracker

| Phase | Name | Size | Status |
|---|---|---|---|
| 0 | Groundwork (accounts, server check) | S | ◐ tooling ready, manual steps left (`docs/phase0.md`) |
| 1 | Host foundation (Debian, Incus, networks) | M | ◐ code ready, run on the server (`docs/phase1.md`) |
| 2 | Golden target image: Debian 13 + Xfce | M | ☐ |
| 3 | Broadcast pipeline | M | ☐ |
| 4 | Control plane MVP | L | ☐ |
| 5 | Safety layer — **gate for going public** | L | ☐ |
| 6 | Governance and time travel | L | ☐ |
| 7 | Closed beta → soft launch | M | ☐ |
| 8 | Roguelike layer + public site | L | ☐ |
| 9 | Depth (driver's seat, macros, constitution, OCR click, chaos) | M–L | ☐ |
| 10 | Growth and 24/7 operations | M | ☐ |
| 11 | New targets and formats | ongoing | ☐ |

---

## 2. Vision and pillars

**One-liner:** a roguelike where the character is an operating system and the player is the whole chat.

Pillars, used to settle design arguments:

1. **Chat is the player.** Every visible change on screen comes from chat (or from a rule chat voted for). The streamer hosts and moderates, never plays.
2. **Every failure is content.** Crashes, wipes and bad votes are the show. Death is celebrated (death screen, Graveyard), not hidden.
3. **Readable over a 5-second delay.** Every mechanic must work when chat sees the screen 3–6 s late: votes, grid cells, queues, on-screen confirmation.
4. **Safe by construction.** The target can't reach the internet freely, can't starve the stream, and anything that ends up on screen passes a filter. Moderators can black out the screen in under a second.
5. **Swappable machine.** The OS behind the screen can change weekly without touching the bot, the overlay or the stream.

---

## 3. Constraints and assumptions

| Constraint | Consequence |
|---|---|
| No nested virtualization (no `/dev/kvm`) | Linux targets run as **system containers** at native speed. Other OSes run in **QEMU software emulation (TCG)**: several times slower; XP-era is fine, Windows 7 is sluggish, 10/11 is unusable. |
| 8 vCPU, no GPU | Software video encoding (x264). **720p30** is the default; 1080p only if benchmarks allow. CPU must be split so chat can't starve the encoder. |
| 16 GB RAM | One desktop target with ~8 GB, or two light targets. |
| 320 GB SSD | Snapshots must be copy-on-write (btrfs). Keep an eye on snapshot growth. |
| netcup acceptable use policy | Any abuse report (DDoS, spam, scanning) can lock the server. The target **never** gets unfiltered internet. |
| YouTube viewers are 3–6 s behind | Mechanics are vote/queue based; overlay acknowledges every command instantly. |
| Discord bots can't Go Live | Video goes to YouTube only. Discord is the control room, the community, and (later) an audio relay. |
| Solo developer, EU (France) | Small, shippable phases; GDPR applies to stored usernames and logs. |

Assumption to check in Phase 0: whether the server is a **VPS** (shared vCores) or a **Root Server** (dedicated cores). On a VPS, CPU steal time can cause stream stutter no matter how well things are pinned.

---

## 4. Key decisions

These are the defaults. Change them deliberately and log it in section 15.

| # | Area | Decision | Why |
|---|---|---|---|
| D1 | Host OS | **Debian 13 (trixie)**, minimal | Stable, packages Incus (6.0 LTS; the Zabbly repo has newer releases if needed). |
| D2 | Isolation | **Incus** for everything: system containers for targets, broadcast, control plane and egress proxy. QEMU (TCG) runs *inside* an unprivileged container for non-Linux targets. **No Docker on the host** (its firewall rules break Incus bridges). | One tool, unprivileged by default, per-instance CPU pinning, quotas, network ACLs, projects with restrictions. |
| D3 | Storage | Incus **btrfs** storage pool (loop-backed to start, ~200 GiB) | Instant copy-on-write snapshots and per-container disk quotas. |
| D4 | Target interface | Every target exposes **VNC** (screen + input), **raw PCM audio over TCP**, and a **power/snapshot API** (Incus API for containers, QMP for QEMU). | The rest of the system never needs to know which OS is running. |
| D5 | Control plane | **Python 3.13, asyncio, one process ("brain")** + SQLite. Libraries: `discord.py`, `grpcio` (YouTube chat stream), a VNC client (`asyncvnc` or `vncdotool` — evaluate; a minimal RFB client is a fallback), `qemu.qmp`, `httpx` (Incus REST), `pytesseract`, FastAPI/uvicorn, `pydantic`. | Best library coverage for this mix; one process is easiest to debug. Split into services only if needed. |
| D6 | Overlay | **One web page** (Vite + TypeScript) containing noVNC (view-only) and the HUD, fed by a websocket from brain. | The whole stream layout is HTML; easy to iterate and theme. |
| D7 | Broadcast | **Xvfb + Chromium kiosk + ffmpeg (x264) → MediaMTX → forwarder to YouTube (RTMPS)**. 720p30, ~4 Mbps. | Lighter than headless OBS; MediaMTX decouples encoding from the YouTube connection and also feeds clips/previews. |
| D8 | YouTube chat | **Official `liveChatMessages.streamList`** (gRPC server-push). Fallback: `chat-downloader`. Feedback goes on the overlay, **not** as chat replies. | Low latency, official, no polling. Chat replies cost quota and spam chat. |
| D9 | Discord | `discord.py` bot: `!` commands in `#control` + slash commands + a button "control panel". Audio relay later (see Phase 11). | Familiar UX for both platforms. |
| D10 | First target | **Debian 13 + Xfce**, desktop at **1024×576** | Fits 1:1 in a 1280×720 stream with room for the HUD; crisp text. |
| D11 | First ruleset | **Roguelike** (runs, lives, permadeath) | Strongest hook; gives every crash a story. |
| D12 | Config as code | All host and image setup as idempotent shell scripts + Incus YAML in this repo. | Server can be rebuilt from scratch after a compromise or reinstall. |

---

## 5. Architecture

### 5.1 Overview

```
                ┌──────────────────────────── host (Debian 13, Incus, nftables) ─────────────────────────────┐
                │                                                                                             │
 YouTube chat ──┼─► brain (project: infra) ───────────── VNC 5900: input + frames ───────────► target-*      │
  (streamList)  │    adapters → moderation → parser       QMP 4444 (QEMU targets only) ─────► (project:      │
 Discord ───────┼─►  → rate limits → filter → referee     Incus API (restricted cert) ──────►  targets)     │
                │    → executor → audit log                                                      │  │        │
                │    FastAPI: overlay websocket,                                                 │  │        │
                │    admin API, layout page                    PCM audio 4712 ◄──────────────────┘  │        │
                │         │ ws                                                                      │        │
                │         ▼                                    proxy 3128 ◄─────────────────────────┘        │
                │  bcast (project: infra)                        │                                           │
                │   Xvfb :99 ← Chromium kiosk (layout page: noVNC view-only + HUD)                           │
                │   websockify → target VNC     audio-in (PCM → limiter → PulseAudio "mix")                  │
                │   ffmpeg x11grab + mix.monitor → x264 → MediaMTX ─┬─► forwarder → YouTube (RTMPS)         │
                │                                                   ├─► clip ring buffer                    │
                │                                                   └─► HLS/WebRTC preview (WireGuard only) │
                │  egress: Squid allowlist proxy ──► internet (allowlisted domains only)                     │
                │  intranet: kiwix-serve, safe static pages, Freedoom, puzzle files                         │
                └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Instances and CPU budget

CPU numbers are vCPU indexes 0–7. "Pinned" uses Incus `limits.cpu` with a range.

| Instance | Project | Role | CPUs | RAM | Disk |
|---|---|---|---|---|---|
| `bcast` | infra | Xvfb, Chromium layout, PulseAudio mix, websockify, ffmpeg encoder, MediaMTX, forwarder, clip recorder | 0–1 (pinned) | 2 GiB | 20 GiB |
| `brain` | infra | Control plane, overlay/admin API, OCR | 2 (pinned) | 1.5 GiB | 10 GiB |
| `egress` | infra | Squid allowlist proxy | 2 (shared) | 256 MiB | 5 GiB |
| `intranet` | infra | kiwix-serve + static pages | 2 (shared) | 512 MiB | 10–20 GiB |
| `target-*` | targets | The machine chat controls | 3–7 (pinned) | 8 GiB | 40 GiB quota |
| host | — | sshd, Incus, nftables, WireGuard, backups | unpinned | ~1.5 GiB | — |

Total RAM ≈ 13.5 GiB, leaving some headroom. Platform War (two targets) needs a re-budget: e.g. targets on 3–4 and 5–7 with 4 GiB each, and a 1080p benchmark (Phase 11).

### 5.3 Networks and ports

Two Incus bridges:

- **`infranet`** (`10.20.0.0/24`, NAT to internet): `bcast` .10, `brain` .11, `egress` .12, `intranet` .13 (fixed addresses).
- **`chatnet`** (`10.30.0.0/24`, **no NAT, no IPv6, no DNS**): targets only.

Traffic between them is **routed through the host**, so every packet crosses the host firewall (`infra/host/nftables.nft.tmpl`, default drop), which allows only these flows (implemented in Phase 1, see `docs/phase1.md`):

| From | To | Port | Purpose |
|---|---|---|---|
| brain | target | 5900/tcp | VNC: inject input, grab frames (OCR, thumbnails, watchdog) |
| bcast (websockify) | target | 5900/tcp | VNC: view-only video for the layout page |
| bcast | target | 4712/tcp | Raw PCM audio pull |
| brain | target (QEMU host container) | 4444/tcp | QMP (power, snapshots, panic events) |
| target | egress | 3128/tcp | HTTP(S) proxy, allowlisted domains only |
| target | intranet | 80/tcp | Local "internet": offline Wikipedia, safe pages |
| brain | host Incus API | 8443/tcp (infranet) | Restricted certificate, project `targets` only |
| bcast | internet | 443/tcp | YouTube RTMPS ingest |
| brain | internet | 443/tcp | Discord gateway, YouTube API |
| admin laptop (WireGuard) | brain | 8080/tcp | Admin dashboard, previews |
| internet | host | 443/tcp | Public static site (Caddy) |
| internet | host | 22/tcp, 51820/udp | SSH (keys only), WireGuard |

On top of the firewall, target NICs have `security.port_isolation` (targets can't reach each other), MAC/IPv4 anti-spoofing, and bandwidth caps (`limits.egress` / `limits.ingress`). The firewall is one table that never flushes the ruleset, so it coexists with the tables Incus manages. `infra/tests/net-isolation.sh` checks all of this, including a packet capture on the public interface (a no-NAT network can leak packets that never get replies).

### 5.4 Trust boundaries

- **The target is hostile.** Chat has root inside it. Its VNC server, audio stream, process list, files and any agent running inside it are attacker-controlled. Everything that talks to a target:
  - runs in an unprivileged container, never on the host;
  - applies timeouts and size limits on everything it reads;
  - never executes, evaluates or mounts anything coming from the target;
  - is kept updated (VNC client, Chromium/noVNC, ffmpeg).
- **Chat messages are hostile.** Strict grammar parser, length limits, and no shell anywhere in the path (use APIs and argument arrays).
- **Secrets** (stream key, Discord token, Google credentials, Incus client certificate) exist only in `bcast`/`brain`, in `0600` files under `/etc/chat-uses/secrets/`. Nothing on `chatnet` can reach them.
- **brain's Incus access** uses a **restricted trust certificate limited to the `targets` project**, and that project has `restricted=true` (no privileged containers, no device passthrough). A bug in brain must not equal root on the host. ⚠️ Verify the exact `incus config trust` flags for your Incus version.

### 5.5 Video and audio pipeline (in `bcast`)

1. **Display:** `Xvfb :99 -screen 0 1280x720x24`.
2. **Layout:** Chromium in kiosk mode at 1280×720 on `:99`, loading `http://brain:8080/layout`. The page embeds noVNC (view-only, connected through websockify to the target's VNC) and the HUD.
3. **Cursor:** noVNC shows the remote cursor as a *local* CSS cursor, which a headless capture won't show (⚠️ verify in Phase 3). Plan: the overlay draws its own large cursor at the position the injector last set, labelled with who moved it. This is better for viewers anyway.
4. **Audio in:** a service reads raw PCM (s16le, 48 kHz, stereo) from `target:4712`, passes it through a limiter and volume cap (ffmpeg `alimiter`), and plays it into the PulseAudio null sink `mix`. It reconnects forever. Chromium's sound effects (vote passed, death jingle) also go to `mix`.
5. **Encode** (sketch, tune in Phase 3):
   ```
   ffmpeg -f x11grab -framerate 30 -video_size 1280x720 -draw_mouse 0 -i :99 \
          -f pulse -i mix.monitor \
          -c:v libx264 -preset veryfast -b:v 4000k -maxrate 4000k -bufsize 8000k \
          -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p \
          -c:a aac -b:a 160k -ar 48000 \
          -f rtsp rtsp://127.0.0.1:8554/live
   ```
   Try with and without `-tune zerolatency` (lower latency vs better quality).
6. **MediaMTX** receives `live` and serves it locally (RTSP/HLS/WebRTC).
7. **Forwarder:** `ffmpeg -i rtsp://127.0.0.1:8554/live -c copy -f flv <YouTube RTMPS URL>/<key>` in a restart loop. If YouTube drops, only this process restarts; the encoder keeps running. Copy the ingest URL from YouTube Studio (it's `rtmps://a.rtmps.youtube.com/live2` at time of writing).
8. **Clip ring buffer:** `ffmpeg -i rtsp://…/live -c copy -f segment -segment_time 10 -segment_wrap 12 …` keeps the last 2 minutes on disk for event clips (Phase 10).

All of these are systemd units with `Restart=always`. If brain dies, the layout shows a "controls offline" card and the stream continues.

### 5.6 Overlay layout (1280×720)

```
┌──────────────────────────────────────────────┬─1─────────────┐
│                                              │2  RUN #47      │
│        TARGET SCREEN 1024×576 (1:1)          │3  ♥♥♥ lives    │
│                                              │4  ⏱ 02:13:44   │
│                                              │5  ANARCHY ◄▮► │
│                                              │6  DEMOCRACY    │
│                                              │7  ── vote ──   │
│                                              │8  REWIND? 7/10 │
│                                              │9  ⏳ 0:21      │
├A─B─C─D─E─F─G─H─I─J─K─L─M─N─O─P───────────────┤               │
│ @alice  !type hello      @bob   !click F7.5  │  achievements │
│ @carol  !key ctrl+alt+t  …                   │  driver seat  │
│ democracy tally bars / ticker: !help         │  timeline ▢▢▢ │
└──────────────────────────────────────────────┴───────────────┘
```

- Grid **labels sit outside the target area** (letters under it, numbers to its right), so they never hide content. Grid lines flash over the target for 3 s after each click or on `!grid`.
- Full-screen states: Starting soon, BRB / panic, content hold, death screen, run start.
- For other target resolutions (e.g. retro 800×600, 640×480), the layout computes the target box and scales down only if needed.
- Test legibility on a phone before going live.

### 5.7 Control plane (brain) modules

```
control/src/chatuses/
  adapters/      youtube.py (streamList gRPC, resume token), discord.py, twitch.py (later)
  identity.py    unified user IDs (yt:<channelId>, dc:<userId>), presence & eligibility
  parser.py      strict grammar → Command objects
  safety/        textfilter.py, screen_ocr.py, panic.py, moderation.py, audio_monitor.py (later)
  referee/       modes.py (anarchy/democracy meter), votes.py (generic vote engine),
                 ratelimit.py, laws.py, driver.py, macros.py
  executor/      vnc_injector.py, keymap.py, grid.py, ocr_click.py, chaos.py (input transforms)
  targets/       base.py (interface), incus_target.py, qemu_target.py
  game/          runs.py, lives.py, achievements.py, points.py
  watchdog.py
  snapshots.py
  api.py         FastAPI: /layout, /ws/overlay, admin endpoints, /metrics
  store.py       SQLite (WAL) access
  sim/           chat simulator (fake users) for load and vote tests
```

**Command flow:** chat event → moderation gate (banned? lockdown?) → parser → per-user and global rate limits → text filter → referee (anarchy: queue / democracy: ballot / power: vote) → executor queue (serialized, global cap) → injector → audit log + overlay event.

**Target interface** (`targets/base.py`):

```python
class Target(Protocol):
    async def health(self) -> Health: ...            # running? VNC up? booted?
    async def frame(self) -> Image: ...              # current screen via VNC
    async def send_keys(self, combo: KeyCombo) -> None: ...
    async def type_text(self, text: str) -> None: ...
    async def pointer(self, x: int, y: int, buttons: int) -> None: ...
    async def reboot(self) -> None: ...              # graceful
    async def reset(self) -> None: ...               # hard power cycle
    async def snapshot_create(self, name: str) -> SnapshotRef: ...
    async def snapshot_restore(self, ref: SnapshotRef) -> None: ...
    async def snapshot_delete(self, ref: SnapshotRef) -> None: ...
```

### 5.8 Data model (SQLite)

| Table | Key columns |
|---|---|
| `users` | id (`yt:…`/`dc:…`), display_name, display_name_safe, first_seen, last_seen, points, banned_until, roles |
| `commands` | id, ts, user_id, raw, parsed_json, mode, outcome (executed/rejected/filtered/rate_limited), run_id, target |
| `votes` | id, kind, proposer, opened_at, closed_at, params_json, tally_json, eligible_count, result |
| `runs` | id, number, ruleset, target_profile, started_at, ended_at, lives_start, lives_used, cause, killer_user_id, final_frame_path |
| `snapshots` | id, run_id, kind (auto/named/panic), name, created_at, backend_ref, thumb_path, deleted_at |
| `achievements` | id, run_id, key, at, credited_users_json |
| `laws` | key, value, bounds, amended_at, amendment_vote_id |
| `macros` | name, expansion_json, created_by, vote_id, active |
| `events` | id, ts, type, payload_json — append-only log of everything (debugging, replays, site generation) |
| `incidents` | id, ts, trigger, frame_path, actions, resolved_by, notes |

### 5.9 Repository layout

```
chat-uses/
  PLAN.md                 this file
  README.md
  infra/
    host/                 bootstrap.sh, nftables.conf, wireguard/, unattended-upgrades
    incus/                networks/, acls/, projects/, profiles/ (YAML)
    egress/               squid.conf, allowlist.txt
    intranet/             kiwix + static pages
  images/
    debian-xfce/          build.sh, overlay files (systemd units, Firefox policies, wallpaper)
    qemu-host/            container template running QEMU for retro targets
    retro-*/              per-OS image notes and build steps
  broadcast/              systemd units and configs for Xvfb, Chromium, ffmpeg, MediaMTX, forwarder
  overlay/                Vite + TypeScript layout page
  control/                Python package (brain) + tests/
  rulesets/               roguelike.yaml, living-pc.yaml, platform-war.yaml, …
  achievements/           achievement definitions (YAML)
  site/                   public static site generator
  docs/                   runbooks, redteam.md, incidents/, privacy.md
```

---

## 6. Game design spec

### 6.1 Command reference

**Input commands (tier 0)**

| Command | Example | Notes |
|---|---|---|
| `!type <text>` | `!type sudo apt install cowsay` | ≤ 40 chars, filtered, no Enter |
| `!enter` `!tab` `!esc` `!space` | | |
| `!bksp [n]` | `!bksp 5` | n ≤ 10 |
| `!up` `!down` `!left` `!right [n]` | `!down 3` | n ≤ 10 |
| `!key <combo> [combo…]` | `!key ctrl+alt+t` | ≤ 4 combos |
| `!click <cell>[.<1-9>]` | `!click F7.5` | sub-cell is a phone keypad (1 = top-left, 5 = centre) |
| `!rclick` `!dclick` `!mclick <cell>` | `!dclick C2` | right, double, middle |
| `!move <cell>` | `!move K3.9` | |
| `!drag <cell> <cell>` | `!drag B2 H6` | |
| `!scroll up\|down [n]` | `!scroll down 5` | n ≤ 10 |
| `!click "<text>"[#n]` | `!click "Settings"` | Phase 9: OCR-located click |
| `!<macro>` | `!term` | Phase 9: chat-defined macros |

**Meta commands**

| Command | Effect |
|---|---|
| `!anarchy` / `!democracy` | Push the mode meter |
| `!reboot` / `!reset` | Propose a power vote (graceful / hard) |
| `!rewind [n]` / `!load <name>` | Propose rewinding to the n-th last snapshot / a named save |
| `!save <name>` | Propose a named save |
| `!newrun` | Propose abandoning the run |
| `!yes` / `!no` | Vote in the current power vote |
| `!veto` | Cancel during a veto countdown |
| `!impeach` | Propose removing the current driver |
| `!macro <name> = <cmd>; <cmd>…` | Propose a macro (Phase 9) |
| `!amend <law> <value>` | Propose a rule change (Phase 9) |
| `!grid` | Flash the grid |
| `!help` `!rules` | Overlay shows the help card |
| `/me` (Discord only) | Ephemeral: your points, rank, history |

**Moderator commands** (Discord slash, role-gated; also on the admin dashboard): `/panic`, `/resume`, `/rewind-to <snapshot>`, `/freeze`, `/unfreeze`, `/ban <user> <duration>`, `/timeout`, `/lockdown on|off`, `/mode anarchy|democracy`, `/ruleset <name>`, `/say <text>` (overlay announcement), `/endvote`.

### 6.2 Grid

- 1024×576 target → **16 columns (A–P) × 9 rows (1–9)** of 64 px cells.
- Sub-cells: each cell split 3×3, numbered like a phone keypad, ≈ 21 px precision. `F7` alone = centre (same as `F7.5`).
- Other resolutions: `cols = round(width / 64)`, `rows = round(height / 64)`, cell size = width/cols × height/rows.
- Grid math is pure and unit-tested.

### 6.3 Modes: anarchy and democracy

- **Anarchy:** every valid input executes in arrival order, subject to rate limits (per user: 1 command / 2 s, burst 3; global: 8 inputs/s).
- **Democracy:** every 12 s (law `democracy_window_s`), identical normalized input commands are tallied; the winner executes. Ties go to the earliest. The overlay shows live tally bars.
- **Meter:** 0–100, starts at 50. `!anarchy` −2, `!democracy` +2, max once per user per 10 s. Switch to democracy at ≥ 80, back to anarchy at ≤ 20 (hysteresis). After a switch, the mode is locked for 2 minutes. The meter drifts 1 point / 10 s toward the centre.

### 6.4 Action tiers and vote rules

| Tier | Actions | Pass threshold | Quorum (fraction of active, floor) | Window | Veto countdown | Cooldown | Cost |
|---|---|---|---|---|---|---|---|
| 0 | Input | — | — | — | — | rate limits | — |
| 1 | `!reboot`, `!save` | ≥ 50 % | 15 %, ≥ 2 | 30 s | — | 2 min | — |
| 2 | `!reset` | ≥ 60 % | 20 %, ≥ 3 | 30 s | — | 5 min | — |
| 3 | `!rewind`, `!load` | ≥ 66 % | 25 %, ≥ 3 | 30 s | 30 s | 10 min | 1 life |
| 4 | `!newrun` | ≥ 75 % | 35 %, ≥ 4 | 45 s | 60 s | 60 min | ends the run |
| meta | `!impeach` | ≥ 50 % | 20 %, ≥ 2 | 20 s | — | 5 min | — |
| meta | `!macro` | ≥ 50 % | 15 %, ≥ 2 | 60 s | — | 5 min | — |
| meta | `!amend` | ≥ 75 % | 30 %, ≥ 4 | 90 s | 60 s | 60 min | — |

- One power vote at a time. Others are rejected with an overlay toast.
- The proposer counts as a yes.
- **Veto:** during the countdown, the action is cancelled if vetoes ≥ max(3, ⅓ × yes votes).
- All numbers are **laws** (configurable per ruleset, some amendable by chat; see 6.10).

### 6.5 Who counts (anti-raid)

- **Active users** at time *t*: users who sent any message in the last 5 minutes and aren't banned.
- **Eligible voter** for a vote opened at *T*: active at *T* **and** present in this stream session since at least *T − 2 min*. A raid of brand-new accounts can't swing a vote instantly.
- **Quorum** = max(floor, ceil(fraction × active)).
- **Pass** if yes ≥ quorum **and** yes / (yes + no) ≥ threshold.
- **Low population:** floors still apply to tiers ≥ 3, so one person alone at 4 am can't end a run. The watchdog handles genuinely broken machines (6.6).

### 6.6 Snapshots, rewind and lives

- **Auto snapshots** every 15 min (law: 10–30), only if the target is healthy and the screen changed since the last one. Keep a ring of 8 per run.
- **Named saves:** 3 slots per run, names `[a-z0-9-]{1,16}` and filtered. Saving into a full set overwrites the oldest named save.
- **Rewind targets:** `!rewind` = last auto snapshot, `!rewind n` = n-th back, `!load name`.
- **Timeline** on the overlay: thumbnails of the last 6 snapshots with labels.
- **Lives:** a run starts with 3. Each rewind (voted or watchdog revive) costs 1. Some achievements grant +1. At 0 lives, the next death is permanent.
- **Watchdog revive:** when the target is unhealthy (6.7), a **death vote** opens for 60 s: `!revive` (rewind to the last healthy snapshot, −1 life) vs `!rip` (end the run). Default with no votes: revive if lives remain.
- **Backends:**
  - Incus container: `incus snapshot restore` works on a running container (about 1 s on btrfs in the Phase 1 simulation). ⚠️ On Debian 13's Incus it is refused in the restricted `targets` project; see D19 for the two tested ways around it, to be chosen in Phase 6.
  - QEMU: QMP `snapshot-save` / `snapshot-load` jobs on qcow2 (includes RAM, so open windows come back). The guest pauses briefly; the overlay shows "💾 saving".
- **Disk:** prune auto snapshots of dead runs after their Graveyard entry is written. Alert at 70 % pool usage.

### 6.7 Runs (roguelike layer)

- **States:** provisioning → alive ⇄ rewinding → dying (death vote) → dead → archived.
- **Health checks** (watchdog, every 10 s): instance running; VNC reachable; screen changed within 2 min *after* inputs were sent; booted to desktop within 3 min of a reboot (OCR/frame heuristic); QEMU `GUEST_PANICKED` event (Linux guests with `-device pvpanic`).
- **Cause of death:** the watchdog reason (no boot, display server dead, VNC dead, kernel panic, frozen) or "abandoned by vote".
- **"Final blow":** the last command executed within 60 s before the last healthy check. Shown as "probably killed by @user". It's a joke, not an accusation; keep the tone light.
- **Death screen:** full-screen card for 20 s with the cause, uptime, lives used, last 10 commands, final blow and a jingle. Posted to Discord `#graveyard` and to the site.
- **New run:** created from the latest golden image, run number +1, overlay shows "RUN #48 BEGINS".

### 6.8 Achievements

Defined in `achievements/*.yaml`, checked every 30 s by brain:

```yaml
- key: doom_guy
  name: "Doom Guy"
  description: "Get Freedoom running"
  check: { type: process, pattern: "^(chocolate-doom|crispy-doom|prboom-plus)$" }
  reward: { points: 50, lives: 1 }
- key: survivor_6h
  name: "Survivor"
  description: "6 hours without a rewind"
  check: { type: uptime_since_rewind, hours: 6 }
  reward: { points: 100, lives: 1 }
```

Check types: `process` (list processes via `incus exec … ps`), `file_exists`, `package` (dpkg), `ocr_text` (screen contains text), `uptime_since_rewind`, `http` (target serves a page on :80), `custom` (Python plugin). Results come from a hostile target, so chat can fake some of them. That's accepted (and funny).

Credit goes to users who sent at least one command in the 10 minutes before the unlock.

Starter set (≈ 15): Hello World, Wallpaper Artist, Doom Guy, Encyclopedist (offline Wikipedia open), Package Manager, Compiler (built and ran a C program), Pythonista, Web Master (HTTP server on :80), Terminal Aesthetic (cmatrix running), Dark Mode, Survivor 1 h / 6 h / 24 h, Minimalist (empty desktop), Archivist (named all 3 save slots).

### 6.9 Driver's seat

- Every 10 min (law), pick a random eligible user who is active, unbanned, has sent ≥ 3 commands this session and hasn't driven in the last 60 min.
- They have 15 s to accept by sending any command, then get **60 s of exclusive input**. Everyone else's input is ignored; meta votes still work.
- `!impeach` (meta vote) removes the driver.
- Discord users get pinged; YouTube users are announced on the overlay.

### 6.10 Macros and constitution (Phase 9)

**Macros:** `!macro term = key ctrl+alt+t` or `!macro hello = type hello; enter`.

- Name `[a-z][a-z0-9]{1,11}`, filtered, can't shadow built-ins.
- Expansion: ≤ 8 tier-0 commands, no macro calls (no recursion), typed text filtered.
- Max 64 active macros. `!unmacro <name>` is also a vote.
- Macros persist across runs: chat's language grows over time.

**Laws** (shown on the overlay and the site as "The Laws"):

| Amendable by chat (with bounds) | Not amendable ("Bill of Rights") |
|---|---|
| `democracy_window_s` 8–30 | All filters and blocklists |
| `auto_snapshot_min` 10–30 | Rate limits |
| `driver_interval_min` 5–30, `driver_duration_s` 30–120 | Network policy and allowlist |
| Meter switch points 70–90 / 10–30 | Bans, panic, moderator powers |
| Tier 1–3 thresholds within ±10 % of default | Tier 4 (`!newrun`) rules |
| `lives_start` 1–5 (next run) | Eligibility (anti-raid) rules |

Amendments are numbered ("Amendment #7, proposed by @user") and logged.

### 6.11 Points and chaos modifiers

- **Points:** +1 per active minute (max 1/min), +2 per executed command (max 10/min), achievement bonuses. Never negative. Shown on the site leaderboard and via `/me`.
- **Chaos modifiers**, bought with points, triggered by random events, or mapped to Super Chat tiers. Max one active at a time, global cooldown 5 min. **Implemented in the injector or overlay, never by changing the target**, so they're always reversible and can't break a run:

| Modifier | Where | Duration |
|---|---|---|
| Inverted mouse (mirror coordinates) | injector | 60 s |
| Dvorak / AZERTY keyboard (remap keys) | injector | 60 s |
| Caps Lock glued | injector | 60 s |
| Drunk mouse (±20 px jitter on clicks) | injector | 60 s |
| Input lag +5 s | executor | 60 s |
| Shuffled grid labels | overlay + grid | 60 s |
| Upside-down screen | overlay (CSS rotate) | 30 s |
| Earthquake (screen shake) | overlay | 15 s |
| Blackout (chat works blind) | overlay | 30 s |

- Paid triggers buy **chaos only**, never votes, rewinds or power actions.

### 6.12 Rulesets

A ruleset is a YAML file that sets laws, enabled commands and features:

```yaml
# rulesets/roguelike.yaml
name: roguelike
target_profile: debian-xfce
features: [anarchy, democracy, power_votes, snapshots, lives, achievements, driver_seat, chaos]
laws:
  democracy_window_s: 12
  auto_snapshot_min: 15
  lives_start: 3
  driver_interval_min: 10
  driver_duration_s: 60
tiers:
  rewind: { threshold: 0.66, quorum_fraction: 0.25, quorum_floor: 3, window_s: 30, veto_s: 30, cooldown_min: 10 }
  newrun: { threshold: 0.75, quorum_fraction: 0.35, quorum_floor: 4, window_s: 45, veto_s: 60, cooldown_min: 60 }
commands:
  disabled: []
```

Other rulesets override what they need (e.g. `living-pc.yaml` disables `newrun` and `rewind` except for moderators).

---

## 7. Safety and moderation spec

### 7.1 Threats and mitigations

| Threat | Mitigation | Test (7.7) |
|---|---|---|
| Outbound abuse (DDoS, spam, port scans, torrents, mining) | No NAT on `chatnet`; Squid allowlist; Incus ACLs + nftables; per-NIC bandwidth caps | 5, 6, 7 |
| NSFW / illegal content on screen | Domain allowlist, Firefox `WebsiteFilter`, no image-heavy domains, screen OCR, moderators, panic | 8, 16 |
| Slurs typed or displayed | Text filter with rolling buffer, OCR, filtered overlay text | 8, 9 |
| Offensive usernames on overlay | Filter display names; fall back to `user#1234` | 9 |
| Doxxing | Regex filters for phone numbers, emails, addresses; no personal data in the target; streamer never logs into anything inside it | 8 |
| Copyrighted audio (Content ID) | No music sites in the allowlist; original/free audio only | — |
| Ear-splitting audio | Limiter + volume cap in the audio-in service | 13 |
| Container escape | Unprivileged containers, restricted project, no nesting, no devices, no host mounts, default AppArmor/seccomp, timely kernel updates | 21 |
| Resource exhaustion (fork bomb, CPU, RAM, disk) | `limits.processes`, pinned `limits.cpu`, `limits.memory` without swap, root disk quota, disk I/O priority | 1–4 |
| Stream starvation | Encoder on dedicated cores; watch steal time | 2 |
| Attacks on infra through VNC/audio/agent data | Unprivileged infra containers, timeouts, size limits, updates | 7 |
| Command injection via chat | Strict parser, no shell, argument arrays only | 14 |
| Raids / vote brigading | Eligibility delay, quorums, lockdown mode, YouTube slow mode | 15 |
| Account takeover (stream key, bot token) | 2FA everywhere, secrets `0600` in infra only, separate Google Cloud project | — |
| Brain crash | Stream continues with "controls offline"; systemd restart | 17 |

### 7.2 Network egress

- **Default: almost no internet.** The target sees:
  1. **The Intranet** (`intranet` container): kiwix-serve with an offline **no-pictures** Wikipedia (Simple English or a "top articles" ZIM to save disk), safe static pages (a retro-style homepage, puzzle files for the escape-room format, Freedoom WADs, sample files).
  2. **Debian package mirrors** through Squid (`deb.debian.org`, `security.debian.org`), so chat can install software.
- **Squid** (`egress`): `dstdomain` allowlist for both HTTP and `CONNECT`; everything else denied; `delay_pools` cap of ~2 MB/s; access log kept 14 days.
- Be careful adding domains: live Wikipedia and Wikimedia Commons contain explicit images; GitHub hosts arbitrary content. Each new domain is a decision recorded in the decision log.
- No DNS for targets (the proxy resolves). Target configuration: `http_proxy`/`https_proxy` env, apt proxy config, Firefox enterprise policies (`Proxy` locked, `WebsiteFilter` with `Block: ["<all_urls>"]` + exceptions, homepage = intranet).

### 7.3 Resource limits (target profile `target-desktop`)

| Setting | Value |
|---|---|
| `limits.cpu` | `3-7` |
| `limits.memory` | `8GiB` (restricted projects can't set `limits.memory.swap`, so the host runs without swap) |
| `limits.processes` | `2048` |
| root disk `size` | `40GiB` |
| disk I/O | lower priority than infra (`limits.disk.priority`) |
| NIC `limits.egress` / `limits.ingress` | `20Mbit` |
| `security.privileged` / `security.nesting` | `false` / `false` |
| `boot.autostart` | `true` (infra too) |

### 7.4 Content filtering

**Text filter** (`safety/textfilter.py`), applied to `!type` text, macro names and expansions, save names, amendment text, and every display name shown on the overlay:

1. Normalize: Unicode NFKC, casefold, strip zero-width and combining characters, map leetspeak (`4→a, 3→e, 1→i/l, 0→o, 5→s, $→s, @→a`…), collapse repeated letters, remove separators for a second pass.
2. Match against wordlists for the audience languages (at least EN + FR; decide in Phase 0), plus regexes for URLs, emails, phone numbers, street addresses.
3. **Rolling buffer:** keep the last 200 characters typed into the target (approximating backspaces). Reject a `!type` if appending it would create a match. This stops slurs split across several commands.
4. Rejected commands: overlay shows "filtered", the user gets a strike; 3 strikes in 10 min → automatic 10 min timeout.

**Screen OCR** (`safety/screen_ocr.py`): every 5 s, OCR the target frame with tesseract, run the normalized text through the same blocklists.

- On a hit, enter **content hold**: the overlay covers the target ("🛑 Content check"), input freezes, and moderators get a screenshot in `#mod-alerts` with buttons **[Resume] [Rewind] [Rewind + timeout last 3 typists]**.
- With no moderator action in 3 minutes, rewind automatically to the snapshot before the text appeared. Content holds don't cost lives.
- Run it in **shadow mode** (log only) during the closed beta to tune false positives before enforcing.

**Audio** (later, optional): transcribe target audio with a small whisper.cpp model on one core and run the same filter. Until then, the residual risk is text-to-speech of encoded text; moderators and panic cover it.

**Residual risk:** no automation catches everything (e.g. drawing something offensive pixel by pixel). Human moderators plus the panic button are part of the design, especially in the first months.

### 7.5 Panic

- **Triggers:** `/panic` (Discord moderators), admin dashboard button, a hotkey or Stream Deck calling the admin API over WireGuard, or an automatic OCR hit (content hold variant).
- **Effects within 1 s:**
  1. The overlay fully covers the screen ("We'll be right back").
  2. Target audio is muted in the mix.
  3. The input queue is frozen and flushed; open votes are cancelled.
  4. A private forensic snapshot `panic-<timestamp>` is taken (not restorable by chat).
  5. A post goes to `#mod-alerts`.
- **Exit:** `/resume` (keep state) or `/rewind-to <snapshot>`. Panic never costs lives.

### 7.6 Moderation

- **Roles:** Owner, Moderator (Discord role; YouTube moderators are recognized from chat badges), Regular. Muting uses Discord's built-in timeouts.
- **Cross-platform bans** by unified ID with a duration (`/ban yt:UC… 7d`). Optionally mirror to YouTube's own ban via the API (⚠️ verify quota cost).
- **Lockdown mode** (raid response): only users with ≥ N points or the Regular role can send commands.
- **Night mode** (24/7 phase): stricter laws when no moderator is online (higher quorums, democracy only, lower rate limits).
- **Audit viewer** in the admin dashboard: who sent what, when, and what happened to it.

### 7.7 Red-team suite

Automate what can be automated (`control/tests/redteam/`), and record results in `docs/redteam.md`. Re-run the automated subset before every session after a change.

1. Fork bomb `:(){ :|:& };:` → capped by `limits.processes`; stream unaffected; watchdog recovers.
2. CPU hog (`stress-ng --cpu 64`) → encoder stays at ≥ 1.0× speed, no dropped frames.
3. Disk fill (`dd if=/dev/zero of=/fill`) → quota hit; host and other instances unaffected.
4. Memory hog → OOM inside the target only.
5. `curl https://example.com` (not allowlisted) → denied by proxy; `curl --noproxy '*'` → no route; `ping 1.1.1.1` → no route; DNS to `8.8.8.8` → dropped; IPv6 → none.
6. `nc` to port 25 anywhere → blocked.
7. Scan `infranet` and `chatnet` from the target (`nmap`) → only proxy and intranet ports reachable.
8. Slur via `!type`: whole, split across 3 commands, leetspeak, zero-width characters, and via `echo <base64> | base64 -d` displayed on screen (OCR must catch it).
9. Display name containing a slur → anonymized on the overlay.
10. `sudo rm -rf --no-preserve-root /` → watchdog detects, death vote, revive works.
11. Kill the VNC server inside the target → watchdog restarts it or opens a death vote.
12. Change resolution with `xrandr` → layout copes; watchdog resets it.
13. Full-volume white noise (`speaker-test`) → limiter keeps output at a sane level.
14. 50 simulated users flooding commands, including malformed and very long ones → rate limits hold; brain CPU < 50 %.
15. 30 brand-new simulated accounts vote `!rewind` → ineligible for 2 minutes.
16. Panic end-to-end latency < 1 s (measured on the stream preview).
17. Kill brain → stream continues with "controls offline"; brain is back within 10 s.
18. Kill the forwarder / cut the network for 30 s → reconnects; encoder never restarts.
19. Reboot the host → everything comes back without manual steps.
20. QEMU target: guest kernel panic / BSOD → detected.
21. Escape attempts from the target: `mount`, writes to `/proc/sys`, `modprobe`, raw sockets → denied.

---

## 8. Roadmap (phases)

### Phase 0 — Groundwork · S

Runbook: **`docs/phase0.md`**. Tooling is in the repo; the remaining items are manual.

- [x] Tooling: `infra/host/server-check.sh` (server facts + CPU steal under load), `infra/discord/setup_server.py` (roles and channels, with tests).
- [ ] Check server type (VPS vs Root Server) and measure steal time: `infra/host/server-check.sh`.
- [ ] Choose a channel name and branding; register a domain for the public site.
- [ ] YouTube: enable live streaming on the channel (verification can take up to 24 h). Set defaults: ultra-low latency, not made for kids, slow mode, blocked words list.
- [ ] Google Cloud: a dedicated project, YouTube Data API v3 enabled, an API key (for reading chat) and an OAuth client (for later broadcast management).
  - ⚠️ Gotcha: OAuth apps left in "Testing" status get refresh tokens that expire after 7 days. Move the app to "In production" (an unverified-app warning is fine for personal use).
- [ ] Discord: create the server, application and bot (Message Content intent on, Public Bot off), then run `setup_server.py` to create the channels `#announcements`, `#rules`, `#control`, `#general`, `#theorycraft`, `#graveyard`, `#achievements`, `#clips`, private `#mod-alerts` and `#ops`, the Stage channel "📻 Machine audio" (needs Community enabled), and the roles Owner, Moderator, Regular.
- [ ] Turn on 2FA everywhere; store credentials in a password manager.
- [ ] Decide the audience language(s) (drives wordlists and overlay text).
- [ ] Recruit 2–3 trusted moderators for the beta.

**Done when:** you can get a stream key, the bot token works, and the steal-time result is written in the decision log.

### Phase 1 — Host foundation · M

Runbook: **`docs/phase1.md`**. Code is written and was tested in a simulated host; it still has to run on the real server.

- [x] `infra/host/bootstrap.sh` (idempotent, steps can run alone) with config in `infra/host/host.env.example` → `/etc/chat-uses/host.env`.
- [x] SSH keys only (with a lockout guard and rollback), automatic security updates, timezone, kernel settings.
- [x] nftables host firewall (`infra/host/nftables.nft.tmpl`): default drop, SSH + WireGuard (+ 443 later), routed `chatnet` ↔ `infranet` flows from 5.3. Never flushes Incus's tables.
- [x] WireGuard admin tunnel + `infra/host/wg-add-peer.sh`.
- [x] Incus: btrfs pool (loop-backed, 200 GiB), networks `infranet` (NAT) and `chatnet` (no NAT, no IPv6, no DNS), projects `infra` and `targets` (`restricted=true`, snapshots allowed, caps), profiles `bcast`, `brain`, `egress`, `intranet`, `target-desktop` as YAML in `infra/incus/`.
- [x] `infra/tests/net-isolation.sh` (red-team tests 5–7, with a negative control done during development).
- [ ] Install Debian 13 from the netcup image and run `bootstrap.sh` on the server.
- [ ] Add your laptop to the tunnel.
- [ ] Isolation test passes on the server, and again after a reboot.

**Done when:** the isolation test passes on the server and after a reboot, and `bootstrap.sh` can rebuild the setup from scratch.

### Phase 2 — Golden target image: Debian 13 + Xfce · M

- [ ] `images/debian-xfce/build.sh`: launch `images:debian/13` in project `targets` and install:
  - Xfce, TigerVNC (`Xvnc` at 1024×576, shared sessions allowed, listening on the `chatnet` IP only), dbus-x11;
  - PipeWire/PulseAudio with `module-simple-protocol-tcp` (record only, s16le, 48 kHz, stereo) on port 4712;
  - larger default fonts and cursor; screensaver, lock screen and power management disabled;
  - `firefox-esr` with enterprise policies (proxy locked, allowlist, intranet homepage), apt proxy config;
  - a `chat` user with passwordless sudo (chat is root inside the container; that's the game);
  - systemd units for Xvnc, the Xfce session and the audio TCP server, all auto-restarting;
  - branded wallpaper.
- [ ] Publish it as an Incus image `golden/debian-xfce-v1`; record the version.
- [ ] Measure time to boot to desktop, snapshot time, and restore time.
- [x] ~~Decide whether `incus snapshot restore` needs a stop/start on a running container~~: it doesn't (Phase 1 simulation). Restore in the restricted project needs the D19 workaround.

**Done when:** from a test container on `chatnet` you can see the desktop via VNC and hear audio from port 4712; boot < 30 s and restore < 15 s (or the measured numbers are accepted in the decision log).

### Phase 3 — Broadcast pipeline · M

- [ ] `bcast` container: Xvfb, PulseAudio with null sink `mix`, Chromium kiosk, websockify, ffmpeg encoder, MediaMTX, forwarder, audio-in service. All systemd units with `Restart=always`.
- [ ] Minimal static layout page: noVNC view-only box at 1024×576, placeholder sidebar and bottom strip, a "controls offline" state when the brain websocket is down, Starting soon and BRB scenes.
- [ ] Check how the remote cursor appears; implement the overlay-drawn cursor if needed (5.5).
- [ ] Unlisted YouTube test streams: 1 h, then 6 h while the target runs `stress-ng` on its cores. Measure encoder speed, dropped frames, CPU per component, A/V sync and end-to-end latency in ultra-low-latency mode.
- [ ] Decide final resolution, bitrate and `-tune zerolatency` (record in the decision log).

**Done when:** a 6-hour unlisted stream runs with no dropped frames while the target is under full CPU load.

### Phase 4 — Control plane MVP · L

- [ ] Python project in `control/`: `uv` for dependencies, `ruff`, `pyright`, `pytest`; a GitHub Actions workflow running lint + unit tests.
- [ ] YouTube adapter: video ID set from the admin dashboard → `videos.list` (`liveStreamingDetails.activeLiveChatId`) → `liveChatMessages.streamList` over gRPC with resume tokens and reconnect. ⚠️ Verify the quota cost of `streamList` on the quota page.
- [ ] Discord adapter: `!` commands in `#control`, slash commands, author roles (Moderator, Regular) mapped onto the unified identity.
- [ ] Identity and presence tracking; SQLite store; append-only `events` log.
- [ ] Parser for tier-0 input commands (6.1), with unit tests including malformed and hostile input.
- [ ] Rate limits (per user and global).
- [ ] Anarchy executor, VNC injector, keymap, grid with sub-cells.
- [ ] Overlay HUD: command log with filtered names and platform icons, grid labels and flash, cursor, connection status.
- [ ] Chat simulator (`sim/`): N fake users with configurable behaviour.
- [ ] Develop against a local Incus setup (Linux laptop) or on the server outside stream hours.

**Done when:** on an unlisted stream, you and a friend control the desktop from both YouTube and Discord, and the command-to-visible latency is measured and written down.

### Phase 5 — Safety layer · L — **gate: no public stream before this is done**

- [ ] `egress` container: Squid allowlist + bandwidth cap; target configured to use it.
- [ ] `intranet` container: kiwix-serve with a no-pictures Wikipedia ZIM, safe static homepage, Freedoom files.
- [ ] Text filter with rolling buffer; filtered overlay text and display names; strikes and auto-timeouts.
- [ ] Screen OCR content hold (shadow mode by default).
- [ ] Panic flow end to end, moderator slash commands, cross-platform bans/timeouts, lockdown mode, minimal admin dashboard with audit viewer.
- [ ] Watchdog with all health checks and auto-restart of target services via `incus exec`.
- [ ] Audio limiter.
- [ ] Run the whole red-team suite (7.7). Record results in `docs/redteam.md`. Fix and re-run until all pass.
- [ ] Write the stream rules (`!rules`, YouTube description, Discord `#rules`) and the privacy notice (11).
- [ ] Train moderators: panic, resume, rewind, ban, lockdown.

**Done when:** all 21 red-team tests pass, panic is < 1 s, and at least two moderators have practised every tool on an unlisted stream.

### Phase 6 — Governance and time travel · L

- [ ] Generic vote engine: power votes, eligibility, quorum, veto countdown, cooldowns, overlay vote banner and countdown.
- [ ] Democracy mode, meter with hysteresis and lock period, tally bars.
- [ ] Snapshot manager: auto ring, named saves, rewind/load, thumbnails, overlay timeline; reboot/reset through the target interface.
- [ ] Ruleset loader (YAML) and `laws` table.
- [ ] Discord control panel message with buttons: Yes / No / Veto / Anarchy / Democracy (edit one persistent message; don't post one per vote).
- [ ] Unit tests with a fake clock; simulator scenarios: 50 users, split votes, brigades, low population, ties.

**Done when:** simulator scenarios pass and rewind / reboot / reset votes work on an unlisted stream with friends.

### Phase 7 — Closed beta → soft launch · M

- [ ] Closed beta: unlisted link for Discord members, 2–3 sessions of 2–3 h. OCR in shadow mode; tune filters, rate limits and vote timings.
- [ ] Turn OCR enforcement on.
- [ ] Public soft launch on a **schedule** (e.g. three evenings a week, 3–4 h), with you hosting and at least one moderator online. Not 24/7 yet.
- [ ] Collect per session: peak and average viewers, active chatters, commands/min, votes proposed/passed, panics, content holds (true/false positives), encoder speed, CPU.
- [ ] Weekly retro note in `docs/retros/`: what was fun, what broke, what to change.

**Done when:** 5 public sessions in a row with no moderation incident that reached viewers, then decide on 24/7 (record in the decision log).

### Phase 8 — Roguelike layer and public site · L

- [ ] Runs, lives, death detection, death vote, death screen, new-run flow.
- [ ] Achievements engine + starter set; points.
- [ ] Public static site (regenerated on events, served by Caddy): Graveyard, run pages (timeline thumbnails, last commands, achievements, contributors), leaderboards, The Laws, rules, privacy notice.
- [ ] Discord auto-posts to `#graveyard` and `#achievements`.

**Done when:** a full run lifecycle (start → achievements → rewinds → death → Graveyard on site and Discord) works end to end.

### Phase 9 — Depth · M–L (pick in any order)

- [ ] Driver's seat + impeach.
- [ ] Macros.
- [ ] Constitution and amendments.
- [ ] `!click "text"` with OCR; for Linux targets, an optional accessibility (AT-SPI) helper inside the target (its answers are untrusted; worst case is a misclick).
- [ ] Chaos modifiers (injector/overlay only), points shop, Super Chat mapping.

**Done when:** each feature has simulator coverage and was live-tested in at least one session.

### Phase 10 — Growth and 24/7 operations · M

- [ ] Event clips from the ring buffer (death, achievement, passed rewind/newrun, panic excluded) in horizontal and vertical (9:16) versions. They land in a **review queue** in the admin dashboard; nothing is posted without approval.
- [ ] VOD rotation: YouTube only archives up to 12 h of a stream, so end and start a new broadcast every ~11 h via the API (`liveBroadcasts` insert/bind/transition). ⚠️ Verify quota costs.
- [ ] Timelapse: one screenshot per minute → weekly/monthly timelapse rendered at low priority.
- [ ] Monitoring and alerts to `#ops` via webhook: encoder speed, forwarder up, brain up, disk and pool usage, steal time, YouTube chat stream connected.
- [ ] Backups with restic to off-server storage: SQLite (online backup), golden images (`incus image export`), site data, secrets (encrypted). Do one restore drill.
- [ ] Night mode (7.6) for unmoderated hours.

**Done when:** 7 days of 24/7 streaming without manual intervention.

### Phase 11 — New targets and formats · ongoing

- [ ] **QEMU target type** (`images/qemu-host`): QEMU inside an unprivileged container, PulseAudio → TCP 4712, QMP on TCP 4444 (`chatnet`), `-device usb-tablet` (absolute mouse), `-nic user,restrict=on,guestfwd=…` to reach only the intranet/proxy, `-device pvpanic` + `-action panic=pause` for Linux guests, `snapshot-save`/`snapshot-load`.
- [ ] Retro images, benchmarking boot time and responsiveness for each: FreeDOS, ReactOS, Haiku, KolibriOS, 9front, AROS. Windows only with real licences.
- [ ] **DOSBox-X / 86Box target type:** the emulator window inside an Xvnc display in a container (86Box also needs a BIOS ROM set; check its licensing).
- [ ] **Platform War:** two targets, commands routed by platform, two-pane layout, 1080p benchmark.
- [ ] **Discord audio relay:** a bot streaming the mix into the Stage channel (Stage channels are exempt from Discord's mandatory voice E2EE, DAVE), or a DAVE-capable voice library. ⚠️ Verify whether `discord.py` supports DAVE by then; `@discordjs/voice` does via `@snazzah/davey`, and `dave.py` provides Python bindings.
- [ ] **Discord Activity:** embed the MediaMTX HLS/WebRTC feed and the vote panel inside Discord via the Embedded App SDK. ⚠️ Verify SDK constraints (URL mappings, CSP).
- [ ] **Twitch adapter** (EventSub chat) if you multistream.
- [ ] **Android** (redroid): needs the binder kernel module on the host *and* usually a privileged container, which breaks the isolation model. Only do it with extra review.
- [ ] **Chat vs AI:** a computer-use capable model (e.g. Claude with the computer use tool) driving the same target interface, with a hard spending cap. Check current models and pricing when building it.

---

## 9. Show formats backlog

| Format | Needs | Ruleset notes | Size |
|---|---|---|---|
| **Roguelike OS** (default) | Phases 1–8 | `roguelike.yaml` | — |
| **Platform War** (YouTube vs Discord) | Two targets, routing, 1080p check | Same goal for both; first to the achievement wins | M |
| **Chat installs Arch** | QEMU target (TCG), Arch ISO | Speedrun timer with splits (partitioned, base installed, bootloader, first boot, desktop) | M |
| **Linux From Scratch saga** | Container target, long-running builds | No `newrun`; extra save slots; weekly episodes | M |
| **OS Time Machine** | QEMU retro images | One OS per week; chat votes next week's OS | M per OS |
| **Escape-room OS** | Custom image, puzzles on the intranet | Flags are achievements; multi-day; Discord threads for theories | L |
| **Break the box** | QEMU Linux VM with pvpanic | Leaderboard for fastest kernel panic; auto-rewind with no lives | S |
| **Chat writes a program** | — | Democracy only; only `!type` (one word), `!enter`, `!bksp`; ends with `!run` | S |
| **Chat makes music** | Self-hosted Strudel on the intranet, or Sonic Pi | Audio-focused layout; weekly "album" export | M |
| **Chat vs AI** | Phase 11 AI driver | AI pursues a goal; chat sabotages or gives orders | M |
| **The Living PC** | Timelapse (Phase 10) | Never reset; rewind moderators-only | S |
| **Seasonal events** | Chaos modifiers | Halloween haunted desktop, new-year time capsule | S each |

---

## 10. Operations

### 10.1 Go-live checklist (every session)

- [ ] Automated red-team subset green since the last code or config change.
- [ ] At least one moderator online, or night mode on.
- [ ] Target healthy; fresh snapshot taken.
- [ ] 2-minute unlisted preview: audio levels, overlay, latency, cursor.
- [ ] YouTube broadcast settings: ultra-low latency, not made for kids, chat on, slow mode, blocked words.
- [ ] Panic dry run on the preview.
- [ ] Announcement posted in Discord `#announcements`.

### 10.2 Incident runbooks (write them out in `docs/runbooks/`)

- **Content incident:** panic → assess → rewind → ban/timeout → note in `incidents` → fix the filter → short post-mortem in `docs/incidents/`.
- **Stream down:** check forwarder logs, MediaMTX, encoder speed, server network, YouTube status page.
- **Brain crash loop:** stream shows "controls offline"; check logs; roll back to the last good commit.
- **Suspected compromise:** stop targets, take forensic snapshots, rotate all secrets, rebuild the host from `bootstrap.sh`.
- **Abuse report from netcup:** reply quickly with proxy logs and a description of the egress isolation; pause streams until understood.

### 10.3 Maintenance

- Weekly host updates; reboot in an announced window.
- Monthly golden image rebuild (security updates); new runs use the latest image.
- Quarterly restore drill from backups.
- Watch btrfs pool usage and prune snapshots of archived runs.

---

## 11. Legal and policy

Not legal advice; check anything you're unsure about.

- **Software licences:** free OSes and software, or licences you own. Freedoom instead of commercial WADs. No ROMs or "abandonware" you don't have rights to.
- **YouTube:** Community Guidelines apply to everything on screen and in audio. Avoid copyrighted music. Super Chat-triggered effects must follow YouTube's monetization policies.
- **Discord:** Developer Terms and Policy. No self-bots (user-account automation). Privileged intents enabled in the developer portal.
- **GDPR (EU):** you store platform IDs, display names, command logs, votes and points.
  - Legal basis: legitimate interest (running the game).
  - Retention: raw command logs 90 days; aggregates and leaderboards until deletion is requested.
  - Publish a privacy notice on the site (what, why, retention, contact); handle deletion requests via Discord or email.
  - Don't publish anything beyond what was already public in chat.
- **netcup:** read the acceptable use policy; keep the abuse contact address monitored.

---

## 12. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Delay makes control frustrating | High | Medium | Vote/queue mechanics, instant overlay acknowledgement, ultra-low latency |
| Too few viewers for votes to work | High (early) | High | Low-population rules, scheduled streams, build the Discord community first |
| Moderation workload burns you out | Medium | High | Automation first, lockdown and night modes, scheduled streams before 24/7 |
| CPU steal on a shared VPS | Medium | Medium | Pinning, measure in Phase 0, upgrade to a Root Server if needed |
| YouTube API changes or quota | Medium | Medium | `chat-downloader` fallback; keep API calls minimal |
| Discord voice library lacks DAVE | Medium | Low | Stage channel, or a separate Node relay with `@discordjs/voice` |
| Strike from on-screen content | Medium | High | Phase 5 gate, tiny allowlist, OCR, moderators, panic |
| Server lock from abuse report | Low (with egress isolation) | High | No NAT on `chatnet`, proxy logs, bandwidth caps |
| Scope creep stalls the project | High | High | Ship Phases 0–7 first; everything else waits |

---

## 13. Open questions

Answer these and move the answers to the decision log:

1. Audience language(s)?
2. Channel name and brand?
3. VPS or Root Server, and what is the measured steal time?
4. Scheduled streams only, or aim for 24/7 after Phase 10?
5. Monetization (memberships, Super Chat effects) from day one or later?
6. 720p vs 1080p after the Phase 3 benchmark?
7. Python for the control plane, or TypeScript? (Default: Python; decide before Phase 4.)
8. First retro OS for the Time Machine season?

---

## 14. References

- YouTube `liveChatMessages.streamList`: https://developers.google.com/youtube/v3/live/docs/liveChatMessages/streamList
- YouTube streaming live chat guide: https://developers.google.com/youtube/v3/live/streaming-live-chat
- YouTube API quota costs: https://developers.google.com/youtube/v3/determine_quota_cost
- YouTube live archive limit (12 h): https://support.google.com/youtube/answer/6247592
- Discord voice/video E2EE (DAVE), mandatory since March 2026, Stage exempt: https://discord.com/blog/every-voice-and-video-call-on-discord-is-now-end-to-end-encrypted
- `dave.py` (Python DAVE bindings): https://github.com/DisnakeDev/dave.py
- discord.js voice guide (DAVE via `@snazzah/davey`): https://discordjs.guide/voice
- Incus documentation: https://linuxcontainers.org/incus/docs/main/
- QEMU documentation: https://www.qemu.org/docs/master/
- MediaMTX: https://github.com/bluenviron/mediamtx
- noVNC: https://github.com/novnc/noVNC
- chat-downloader (fallback chat reader): https://github.com/xenova/chat-downloader
- Kiwix (offline Wikipedia): https://kiwix.org
- Freedoom: https://freedoom.github.io
- Strudel (live-coded music in the browser): https://strudel.cc

---

## 15. Decision log

| Date | # | Decision | Reason |
|---|---|---|---|
| 2026-10-12 | D1–D12 | Initial decisions (section 4) | Initial plan |
| 2026-10-12 | D13 | `chatnet` ↔ `infranet` traffic is routed through the host and filtered by one nftables table; infra containers have no NIC on `chatnet`, and Incus network ACLs aren't used | One place to reason about every allowed flow; verified by the isolation test and its negative control |
| 2026-10-12 | D14 | No `limits.memory.swap` on targets; the host should run without swap (bootstrap warns) | Restricted Incus projects treat it as a low-level option and refuse it |
| 2026-10-12 | D15 | Discord moderation uses built-in timeouts; no "Muted" role | Native, no permission-overwrite edge cases |
| 2026-10-12 | D16 | Incus HTTPS API stays off until Phase 4 (brain) | Nothing needs it yet; no listener means no exposure |
| 2026-11-15 | D17 | Install `incus-base` + `dnsmasq-base` + `apparmor` instead of `incus` | Debian's `incus` pulls in QEMU/swtpm/virtiofsd for VMs this server can't run; bridges need dnsmasq and AppArmor confines containers, both only "Recommends" (found in the Debian 13 simulation) |
| 2026-11-15 | D18 | CI runs on Ubuntu 26.04 LTS with `actions/checkout@v7` | Latest LTS runner; v7 runs on Node.js 24 (Node 20 deprecated) |
| 2026-11-15 | D19 | **Open (Phase 6):** how to restore snapshots in the restricted `targets` project. Recommended: a small root-side helper that checks the snapshot's config has no low-level keys (`raw.*`, `security.privileged`…), lifts `restricted.containers.lowlevel` for the restore and blocks it again. Alternative: copy the snapshot into a new container and swap names (keeps the project untouched, but loses the snapshot history on the old instance) | Debian 13's Incus refuses restores there ("Changing `volatile.*` is forbidden"): since the CVE-2026-48751 fix, restore is checked against the low-level rule, which also catches Incus's own `volatile.*` keys. Both options tested in the simulation; never keep `lowlevel` allowed, since brain's certificate (Phase 4) is scoped to this project |
