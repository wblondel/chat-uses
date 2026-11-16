# Red-team suite (PLAN.md 7.7)

Chat will try to break everything. Before going public, and after every
change to the safety layer, attack the stream the way chat would and check
every defence holds. **Phase 5 is done when all 21 pass** (or wait,
explained, for the phase that brings their feature).

## Running it

On the server, on an unlisted stream or before going live:

```bash
cd /opt/chat-uses
sudo infra/tests/redteam.sh --report /tmp/redteam.md
```

It prints a table; paste it into **Results** below with the date and the
commit. `--only 8,13` runs some; `--keep` leaves the throwaway target for a
look; `--net-image` picks the image `net-isolation.sh` launches (its default
comes from images.linuxcontainers.org).

What it does to the real stream:
- **8, 9:** commands from console chat on the real desktop: a terminal
  opens, a listed word is decoded on screen (screen OCR must see it; never
  printed in the output), a name with a listed word shows as `viewer#NNNN`.
- **11:** stops the desktop's VNC server; the watchdog starts it again
  (input is down up to a minute).
- **13:** 8 s of full-scale white noise on stream.
- **14:** 50 console users flood brain for 20 s (the pointer moves).
- **16:** a real panic for a few seconds (`images/brain/panic-drill.sh`).
- **17, 18:** brain and the forwarder killed (systemd restarts them).
- **5-7:** the isolation test stops egress and intranet for a minute.

1-4 and 21 run on a throwaway target (`redteam-target`, chatnet `.250`,
the `target-desktop` profile with 1 GiB of memory and a 4 GiB disk), deleted
afterwards.

## The tests

| # | Attack | Expected | How |
|---|---|---|---|
| 1 | Fork bomb | capped by `limits.processes`; stream unaffected | automated (throwaway target) |
| 2 | CPU hog (64 busy loops) | encoder keeps real time | automated: frames delivered while it runs |
| 3 | Disk fill | stops at the target's quota; host fine | automated |
| 4 | Memory hog | OOM inside the target only | automated |
| 5 | Internet: other sites, no proxy, ping, DNS, IPv6 | all blocked | `infra/tests/net-isolation.sh` |
| 6 | SMTP (port 25) anywhere | blocked | `net-isolation.sh` |
| 7 | Scan infranet and chatnet | only proxy and intranet ports | `net-isolation.sh` |
| 8 | Slur via `!type`: whole, in pieces, leetspeak, invisible characters, decoded on screen | refused; OCR sees the decoded one | automated |
| 9 | Display name with a slur | `viewer#NNNN` on the overlay | automated |
| 10 | `sudo rm -rf --no-preserve-root /` | watchdog notices; death vote and revive | by hand now (below); revive in Phase 6 |
| 11 | Kill the VNC server | the watchdog restarts it | automated (needs systemd in the target) |
| 12 | `xrandr` resolution change | layout copes; reset | by hand (below); reset in Phase 6 |
| 13 | Full-volume white noise | the stream stays near −14 LUFS | automated |
| 14 | 50 users flooding, malformed and long commands | rate limits hold; brain < 50 % CPU | automated |
| 15 | 30 new accounts vote `!rewind` | not eligible for 2 minutes | Phase 6 (votes) |
| 16 | Panic | stream covered and sound muted in < 1 s | automated (`panic-drill.sh`) |
| 17 | Kill brain | stream goes on ("Controls offline"); brain back in 10 s | automated |
| 18 | Kill the forwarder / cut the network 30 s | reconnects; encoder never restarts | forwarder automated; network by hand (below) |
| 19 | Reboot the host | everything comes back by itself | by hand (below) |
| 20 | Guest kernel panic (QEMU) | detected | Phase 10 (QEMU targets) |
| 21 | Escape attempts: modprobe, `/proc/sys`, block devices, kernel log, raw sockets | denied | automated (throwaway target) |

### By hand

- **10:** in a throwaway target (`--keep` after a run, or launch one), run
  `sudo rm -rf --no-preserve-root /`. Expected today: the watchdog reports
  in `#ops` (services down, then VNC down); nothing comes back by itself
  until Phase 6's revive. Delete the target afterwards.
- **12:** on the desktop, `!key ctrl+alt+t`, `!type xrandr -s 1024x768`,
  `!enter`. Expected: the stream shows the smaller desktop, clicks still land
  where the grid says (brain scales to the new size); `xrandr -s 1600x900`
  puts it back.
- **18, the network:** `sudo incus exec bcast --project infra -- ip link set
  eth0 down; sleep 30; sudo incus exec bcast --project infra -- ip link set
  eth0 up`. Expected: the forwarder reconnects to YouTube, the encoder's
  process is the same before and after (`pgrep -f x11grab`), and the layout
  page reconnects to brain.
- **19:** `sudo reboot`. Expected, within a few minutes and without
  touching anything: all containers running, `images/bcast/check.sh` and
  `images/brain/check.sh` passing, the Incus API up (about 30 s after
  Incus), the stream back on YouTube.

## Results

Paste each run here, newest first: date, commit, where, the table.

### 2026-11-16, sandbox (not the server)

The development sandbox, before the first server run. It can't enforce
every limit: its kernel has no cgroup controllers for processes or memory,
its storage pool (`dir`) can't enforce disk sizes or freeze a running
container for a snapshot, and its targets run without systemd. Where that
matters, the line says so; the server run is the one that counts.

| # | Result | Details |
|---|---|---|
| 1 | not run | no `limits.processes` in the sandbox (no pids controller): the runner refuses a fork bomb without a cap. An earlier try without that guard reached 25,329 processes and stalled the stream: the cap matters |
| 2 | FAIL (sandbox) | 64 busy loops: 250 frames in 12 s (270 wanted); the sandbox's 4 shared cores have no pinning to keep bcast's apart |
| 3 | not run | the `dir` pool can't enforce disk sizes: the runner refuses (an earlier try wrote 9 GiB past a 4 GiB quota) |
| 4 | not run | no memory controller: without a cap, the host's OOM killer would choose the victim |
| 5-7 | see step 1 | `net-isolation.sh` passed in step 1's sandbox run (docs/phase5.md); through the runner here, the sandbox's own image store is unreachable without `--net-image debian13-standin` |
| 8 | PASS | typed slurs refused: whole, in 3 pieces by 3 people, leetspeak, invisible characters; decoded on screen, seen by screen OCR in 4 s |
| 9 | PASS | a name holding a slur showed as `viewer#NNNN` |
| 11 | SKIP | no systemd in the sandbox's targets |
| 13 | PASS | full-scale white noise: −14.4 LUFS on the stream |
| 14 | PASS | 50 users, 4,000 messages in 20 s: brain at 9 % CPU, rate limits held |
| 16 | FAIL (sandbox) | screen covered in 0.22 s, sound muted in 0.18 s (step 3's drill); the forensic snapshot can't be taken on the sandbox's pool |
| 17 | PASS | brain killed: back in 3 s, the encoder never stopped |
| 18 | PASS | forwarder killed: running again in 10 s, the encoder never stopped |
| 21 | PASS | denied: modprobe, writing `/proc/sys`, a block device node, host disks, the kernel log, raw sockets for the chat user. Root in the target may open raw sockets inside its own network namespace; the firewall decides where they go |
