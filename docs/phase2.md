# Phase 2 — Golden target image: Debian 13 + Xfce (runbook)

Builds the machine chat controls as a reusable Incus image, then checks it the
way bcast and brain will use it: the desktop over VNC and the sound on a TCP
port.

**Done when** (from PLAN.md): from the host you can see the desktop over VNC
and hear its audio from port 4712, a new target boots to the desktop in under
30 s, and a snapshot restores in under 15 s (or the measured numbers are
accepted in the decision log).

## What you get

Image alias **`chat-uses/debian-xfce`** (plus a dated alias per build):

| Piece | Details |
|---|---|
| Desktop | Debian 13, Xfce, **1600×900** at 120 DPI (shown 1:1 in the 1080p stream, PLAN.md D21), grayscale font smoothing, 32 px cursor, no compositor, no screen lock, branded wallpaper |
| Display | `chat-xvnc.service`: TigerVNC is the X server (`:0`); VNC on port **5900**, no password, shared sessions |
| Audio | `chat-audio.service`: PulseAudio with one sink, `stream`; its sound is served as raw PCM (s16le, 48 kHz, stereo) on port **4712**, record only |
| Session | `chat-session.service`: Xfce as user `chat`, restarted if chat logs out or kills it |
| User | `chat` (uid 1000), passwordless `sudo`: chat is root inside its container |
| Network | apt, shell and Firefox all point at the egress proxy (`EGRESS_IP:PROXY_PORT`); Firefox's home page is the intranet and every other site is blocked by policy |
| Apps | Firefox ESR, terminal, Mousepad, Thunar, nano, curl |

Settings that matter for the stream live in `images/debian-xfce/rootfs/`:
`etc/chat-uses/target.env` (resolution, DPI) and the Xfce defaults in
`etc/xdg/xfce4/xfconf/`. Changing the resolution also changes the stream
layout and click grid (PLAN.md 5.6, 6.2).

**About security:** chat has root inside the container, so it can change any
of these files, including Firefox's policy and the proxy settings. They are
conveniences. What actually contains chat is the network: the host firewall
(Phase 1) and the egress allowlist (Phase 5). VNC has no password because only
bcast, brain and your WireGuard tunnel can reach port 5900.

## Steps

### 1. Update the host

```bash
cd /opt/chat-uses && git pull
sudo infra/host/bootstrap.sh packages profiles   # python3 for the checks, "builder" profile
```

### 2. Build the image

```bash
sudo images/debian-xfce/build.sh
```

It launches a short-lived builder container on `infranet` (it needs the
internet for packages), runs `provision.sh` inside it, and publishes the
result. Expect 5–10 minutes. At the end it prints the image fingerprint and
aliases.

### 3. Check it

```bash
sudo images/debian-xfce/check.sh --screenshot /root/desktop.png
```

It launches a test target (`phase2-check`, `10.30.0.251`) and checks, from the
host:

- VNC answers, and the Xfce desktop starts (with timings from launch);
- the three services are active;
- the framebuffer is 1600×900 and actually shows a desktop (saved as a PNG);
- the audio port streams 192 000 bytes/s, and a 440 Hz tone played inside the
  desktop comes out of it;
- a snapshot is created and restored while running, and the restore undoes a change.

Copy the screenshot to your laptop to look at it (`scp root@SERVER:/root/desktop.png .`).

### 4. Look at it live (optional)

Run the check with `--keep`, then on your laptop, with the WireGuard tunnel up,
open a VNC viewer on `10.30.0.251:5900`. It's the same shared screen chat will
see; anything you type goes to the desktop. Delete the test target afterwards:
`sudo incus delete -f phase2-check --project targets`.

### 5. Record the timings

Put the boot, snapshot and restore times from the check into PLAN.md section 15,
and tick Phase 2.

## Rebuilding, rollback and cleanup

- Rebuild after changing anything under `images/debian-xfce/`; new targets use the
  new image, running ones keep theirs.
- Every build also has a dated alias, e.g. `chat-uses/debian-xfce/20261115-0146`.
  List them with `incus image list`. To roll back:
  `incus image alias delete chat-uses/debian-xfce` then
  `incus image alias create chat-uses/debian-xfce <fingerprint>`.
- Remove old builds with `incus image delete <fingerprint>` (images are uncompressed,
  about 2 GB each, so they publish fast).
- A failed build deletes its builder container; `--keep` leaves it for debugging
  (`incus exec <builder> --project infra -- bash`).

## How the desktop is wired

```
chat-audio.service ── PulseAudio ── sink "stream" ── monitor ──► TCP 4712 (raw PCM)
        ▲ unix socket /run/chat-audio/native (every app's default server)
chat-xvnc.service ─── Xtigervnc :0 1600x900 ──────────────────► TCP 5900 (VNC)
        ▲ X socket /tmp/.X11-unix/X0
chat-session.service ─ startxfce4 as chat (waits for the X socket)
```

All three run as user `chat` and restart on their own if they die.

## What was tested before this landed

In the Debian 13 simulation (Phase 1's simulated server, Incus 7.6 from
Zabbly): `build.sh` built and published the image (1.16 GiB), then `check.sh`
ran against a target launched from it in the restricted `targets` project:

- VNC answered **9.5 s** after launch and the Xfce desktop was up at **10.1 s**;
- the framebuffer is exactly **1600×900**, and the screenshot shows the full
  desktop: wallpaper, panel, dock, icons, 120 DPI text, clock in `TIMEZONE`;
- the audio port streams at the expected rate, and a 440 Hz tone played inside
  the desktop comes out at −11.7 dBFS (silence: nothing at all);
- a snapshot of the stopped desktop restored in **1.4 s** and undid a change;
- Firefox ESR starts, and `about:policies` shows the locked proxy
  (`EGRESS_IP:PROXY_PORT`), the intranet pass-through and the locked homepage.

Bugs it found, now fixed:
- the Xfce defaults overwrote Debian's own files, which removed the failsafe
  session, so the desktop never started (PLAN.md D22);
- the audio probe measured from the moment it connected, but PulseAudio sends
  its first data about a second later. It now measures the steady rate, and
  reports that delay (relevant to audio sync in Phase 3).

**Not tested there** (this sandbox can't run systemd inside containers; the
simulation started the units' own command lines instead), so watch these on the
real server:

- the systemd wiring: unit ordering, `Restart=`, `RuntimeDirectory=`, and the
  machine ID generated on first boot;
- snapshots of a *running* desktop (this sandbox's kernel can't freeze
  containers; `check.sh` tests it on the server);
- DHCP on `chatnet` (test containers used fixed addresses);
- real boot and Firefox start-up times (Firefox's first start took about 40 s
  in the sandbox, which renders everything slowly).
