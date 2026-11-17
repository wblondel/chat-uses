# Phase 10 — Growth and 24/7 operations

PLAN.md Phase 10. Done when the stream runs 7 days 24/7 without manual
intervention. Built so far, in this order:

1. **The host's monitor: alerts in `#ops`** (below).
2. **Night mode** (below): stricter laws while no moderator is around.
3. **Backups to Google Drive** (below), with the restore drill.

Still to build: event clips, VOD rotation, the timelapse.

## Step 1: the host's monitor

`infra/host/monitor.py`, installed by `bootstrap.sh monitor` as
`/usr/local/sbin/chat-uses-monitor` with a timer that runs it every minute.
It runs on the host, not in brain, so it still speaks when brain is down,
and it posts in `#ops` through a Discord webhook, not brain's bot.

**What it watches**, and how long a problem lasts before `#ops` hears it:

| What | Wrong when | Told after |
|---|---|---|
| brain | `/healthz` doesn't answer (`BRAIN_IP:BRAIN_PORT`) | 2 min |
| YouTube chat (from `/healthz`) | stopped, ended, reconnecting, or still looking for the stream's chat | 5 min |
| Discord (from `/healthz`) | stopped, connecting or reconnecting | 5 min |
| Containers (`brain`, `bcast`, `egress`, `intranet`, project `infra`) | not running (or brain or bcast missing) | 2 min |
| The encoder (bcast's journal, a report every 30 s) | no report in 2 minutes, or the last one below 0.97x (behind real time) | 3 min |
| The host's disk (`/`) | 90 % full | at once |
| Incus's storage pool (`POOL_NAME`) | 85 % full | at once |
| CPU steal (`/proc/stat`) | 10 % of the CPU taken by the VPS's neighbours, over the last minute | 5 min |
| Backups (`/var/lib/chat-uses/backup.json`, step 3) | the last one had a problem, none worked for 36 h, no restore drill yet or for 3 months | at once |

A problem is told once (🚨), and once it has been gone for 2 minutes `#ops`
reads "✅ Back to normal after N min" (a problem that comes and goes is told
once, not every minute). A problem shorter than its grace time is never told: a
redeploy of brain or bcast (about a minute) stays quiet. A post Discord refuses is tried
again the next minute. What's normal stays quiet: YouTube waiting for its
quota or for the stream to go live (brain says so in `#ops` itself), chat
"off" (no video or no token set). The target (`chatpc`) isn't the monitor's:
brain's watchdog and the runs look after it. Nor is the forwarder (does the
stream reach YouTube?): bcast reports it to brain, which tells `#ops`
(PLAN.md D67); with brain down, the monitor says so.

It never reads the forwarder's journal (the stream key is in it) and never
prints the webhook's URL, even when Discord refuses a post.

### On the server

1. **Make the webhook.** In Discord: `#ops` → Edit Channel → Integrations →
   Webhooks → New Webhook (call it "chat-uses monitor") → Copy Webhook URL.
   It's a secret: anyone with it can post in `#ops`. Don't paste it in chat
   or a ticket; put it straight on the server:

   ```sh
   sudo install -m 0600 /dev/stdin /etc/chat-uses/secrets/ops-webhook
   # paste the URL, Enter, then Ctrl-D
   ```

2. **Install the monitor** (from the repo, up to date):

   ```sh
   sudo infra/host/bootstrap.sh monitor
   ```

   It installs the script, writes `/etc/chat-uses/monitor.env` (brain's
   address and the pool's name, from `host.env`), starts
   `chat-uses-monitor.timer` and prints what it sees now. Run it again after
   changing `host.env` or pulling a new `monitor.py`.

3. **Check it reaches `#ops`:**

   ```sh
   sudo chat-uses-monitor --test     # "👋 chat-uses monitor: alerts from the host reach #ops."
   sudo chat-uses-monitor --check    # what's wrong now ("all good"), nothing sent
   ```

4. **Try one alert** (optional): `sudo incus stop intranet --project infra`,
   wait 3 minutes: `#ops` gets "🚨 the intranet container isn't running".
   `sudo incus start intranet --project infra`: 2 minutes later, "✅ Back to
   normal".

**Maintenance.** Before a long job that stops things on purpose (a host
reboot is fine: the timer waits 3 minutes after boot), snooze it:

```sh
sudo chat-uses-monitor --snooze 30   # nothing new told for 30 minutes
sudo chat-uses-monitor --snooze 0    # over
```

Problems still there when the snooze ends are told then.

**Its logs:** `journalctl -u chat-uses-monitor` (one line per post that
failed, with the reason). Its memory: `/var/lib/chat-uses/monitor.json`.

### Tests

`infra/host/test_monitor.py` (in CI with the other host tools): every check
on its own; grace times, told once, back to normal, snooze; whole rounds
against a fake `incus`, a fake brain and a fake Discord webhook (a refused
post told again, the URL never printed).

## Step 2: night mode

`control/src/chatuses/safety/night.py`, PLAN.md 7.6. For the hours when no
moderator is watching: stricter laws, by themselves.

**When night falls.** A moderator is around for 15 minutes after anything
they do that brain sees: a message in YouTube chat or `#control` (YouTube's
moderator or owner badge, Discord's Moderator or Owner role), any slash
command. A moderator who watches without typing says `/here` on Discord, or
`!here` in chat: that counts for an hour. When 15 minutes pass with no
moderator around, night falls.

**What changes at night:**

| | Day | Night |
|---|---|---|
| Votes | each its quorum (PLAN.md 6.4: `!rewind` 25 % of the active, at least 3) | twice the floor, half as large a share again (`!rewind`: 37.5 %, at least 6) |
| Mode | the meter decides | democracy; `!anarchy` and `!democracy` are refused ("night mode: democracy only until a moderator is back") |
| Commands | one every 2 s each (3 in a burst), 8 a second in all | one every 4 s each (2 in a burst), 4 a second in all |
| Driver's seat | offered every 10 minutes | not offered |

Thresholds, voting time, veto countdowns and cooldowns stay as they are. A
vote already open keeps its quorum. The site's Laws page shows the day's
numbers and says what the night changes.

**When the day comes back:** the moment a moderator does (a message,
`/here`, any slash command), with the mode chat had before the night
(anarchy if it was anarchy). The stream shows a 🌙 Night mode badge in the
corner and a line in the log each way; `#mod-alerts` gets "🌙 Night mode on
(no moderator around since 02:14)…" and "☀️ Night mode off (Ana is here)".
A moderator's `/mode` still works at night.

**By hand.** `/night on` keeps the night whoever is around (a raid, a
planned unmoderated stretch), `/night off` never brings it (testing alone),
`/night auto` goes back to deciding by itself. Also `chat-brain-admin night
[auto|on|off]`; `chat-brain-admin night` and `status` say where it stands:

```
Night mode: auto, day (a moderator seen: Ana, 21:40; night in 12 min without one)
Night mode: auto, NIGHT since 02:14 (no moderator since 01:59): /here brings the day
Night mode: on (by console): NIGHT since 21:52
```

The setting and when a moderator was last around are kept in brain's
store: a restart (or a redeploy) doesn't bring the day back, nor push the
night back. A new brain counts as a moderator having just left.

### On the server

Nothing to set up: it comes with brain's next deploy
(`images/build.sh brain && images/brain/deploy.sh`), on by itself ("auto").
Then:

1. In Discord, the new `/here` and `/night` show up for moderators (the bot
   syncs its commands when it starts).
2. `chat-brain-admin night` says "auto, day" with your last message, or
   "NIGHT" if no moderator has been around for 15 minutes.
3. Try it: `chat-brain-admin night on`. The stream shows the badge and the
   log line, `#mod-alerts` gets the post, `!anarchy` is refused, a quick
   series of `!click` is slowed. `chat-brain-admin night auto`, then say
   something in `#control`: the day is back.

Testing alone at night? `chat-brain-admin night off`, and `night auto`
afterwards.

### Tests

`control/tests/test_night.py`: night falling and lifting, `/here`'s hour,
on and off kept across a restart, the night's laws, the rate limits, the
referee (pushes refused, the mode forced and given back).
`tests/test_brain.py`: the whole thing through brain (the overlay, the
log, the status, `!here`, `chat-brain-admin night`). `tests/test_discord.py`:
`/here` and `/night`, and every moderator command counting as being around.
The simulator's `night` scenario (in CI): 12 people play while the
moderator falls silent; 15 minutes later the night falls, a `!rewind` that
5 of them would pass by day fails for want of 6, commands slow down; the
moderator speaks and the day and anarchy come back.

## Step 3: backups to Google Drive

`infra/host/backup.py`, installed by `bootstrap.sh backup` as
`/usr/local/sbin/chat-uses-backup` with a timer that runs it every night.
[restic](https://restic.net) encrypts and deduplicates on the server;
[rclone](https://rclone.org) carries the result to Google Drive. Google
stores the backups but can't read them: only restic's password opens them.

**What goes in:**

| What | From | Why |
|---|---|---|
| brain's store | `chat-brain-admin backup` makes a copy (SQLite's online backup, brain keeps running), YouTube's data expired 7 days ahead | bans, runs, points, the event log: what a rebuilt server can't make again |
| The golden image | `incus image export chat-uses/debian-xfce` (`BACKUP_IMAGES`), once per build | rebuilding it from the repo today gives other package versions |
| Settings and secrets | `/etc/chat-uses` (not restic's own password), `/etc/wireguard` | the tokens, the keys, `host.env`, the tunnel's peers |
| The tools' records | `/var/lib/chat-uses` | session reports, the monitor's memory |
| The site's pictures | `/var/www/chat-uses/img` (not its pages) | runs' last screens; brain writes the pages again from its store |

The other images (bcast, brain, egress, intranet) are rebuilt with
`images/build.sh`; the target's snapshots are chat's game, not worth their
size (a new run starts from the golden image).

**How long:** 7 days (`BACKUP_KEEP_DAYS`). After each backup, those more
than 7 days older than it are deleted. That's what keeps YouTube's 30 days:
brain's copy has YouTube's data expired as it will be 7 days later, so by
the time a backup is deleted, nothing in it has passed its 30 days. The
site's pages stay out because they name viewers. The privacy notice says
the backups are kept 7 days, encrypted, on Google Drive.

**#ops hears it** (the monitor reads `/var/lib/chat-uses/backup.json`): a
backup that had a problem (brain's copy, an image, restic itself), no backup
that worked for 36 hours, no restore drill yet, or none for 3 months
(PLAN.md 10.3: a drill a quarter).

### On the server

Do these in order (each needs the one before).

1. **Redeploy brain** with this version, so it has `chat-brain-admin
   backup`: `sudo images/build.sh brain && sudo images/brain/deploy.sh`.

2. **Install the tools:** `sudo infra/host/bootstrap.sh backup monitor`. It
   installs restic and rclone, makes restic's password and says Google
   Drive isn't connected yet (that's expected).

3. **Keep restic's password off the server**, in your password manager:
   `sudo cat /etc/chat-uses/secrets/restic-password`. If the server dies,
   it's the only way to read the backups. Don't paste it anywhere else.

4. **Connect Google Drive.** rclone needs a browser once, and the server
   has none: a tunnel lends it yours. From your computer (over WireGuard,
   as usual):

   ```sh
   ssh -L 53682:127.0.0.1:53682 you@10.99.0.1   # the server's tunnel address
   ```

   Then, in that session:

   ```sh
   sudo rclone config --config /etc/chat-uses/rclone.conf
   ```

   - `n` (new remote), name: **`gdrive`**
   - Storage: **`drive`** (Google Drive)
   - `client_id` and `client_secret`: Enter (empty: rclone's own)
   - scope: **`drive.file`**: rclone sees only the files it makes, not the
     rest of your Drive
   - `service_account_file`: Enter; "Edit advanced config": `n`
   - "Use web browser to automatically authenticate": `y`. rclone prints a
     link (`http://127.0.0.1:53682/auth?state=…`): open it in your
     computer's browser (the tunnel carries it), sign in with the Google
     account that will hold the backups, allow
   - "Configure this as a Shared Drive": `n`; "Keep this remote": `y`; `q`

   No tunnel? Answer `n` to the browser question: rclone prints a command
   (`rclone authorize "drive" "…"`) to run on a computer that has rclone
   (rclone.org/downloads) and a browser, and you paste its answer back.

5. **Make the repository and start the nightly backups:** `sudo
   infra/host/bootstrap.sh backup` again. It makes `chat-uses-backups` in
   your Drive and starts `chat-uses-backup.timer` (every night at
   `BACKUP_TIME`, 04:17, up to 30 minutes later).

6. **The first backup, now:**

   ```sh
   sudo systemctl start chat-uses-backup     # a while: the golden image goes up once
   journalctl -u chat-uses-backup            # "backup: done (snapshot …)"
   sudo chat-uses-backup --check             # the backups, the space they take on Drive
   ```

7. **The restore drill:** `sudo chat-uses-backup --drill`. It downloads the
   latest backup into a temporary folder, checks brain's store (whole, with
   its tables), the secrets, the image (every file readable) and that no
   site page or restic password got in, then deletes it. "all good: this
   backup would rebuild the server" is the drill passed; PLAN.md asks for
   one now and one a quarter (`#ops` reminds you).

**Space:** a free Google account has 15 GB. restic compresses; the golden
image is most of it, and a new build adds one copy for 7 days. `--check`
says how much it takes.

**If Google says the token expired** (`journalctl -u chat-uses-backup`:
"invalid_grant"): `sudo rclone config reconnect gdrive: --config
/etc/chat-uses/rclone.conf` with the tunnel open. With rclone's own client
the token doesn't expire by itself; with a client of your own, its Google
Cloud app must be "In production", or Google ends its tokens every 7 days.

### Restoring for real

On a new server, after `bootstrap.sh` (docs/phase1.md) with your `host.env`:

1. `sudo infra/host/bootstrap.sh backup`, put restic's password back from
   your password manager into `/etc/chat-uses/secrets/restic-password`
   (`sudo install -m 0600 /dev/stdin …`, paste, Enter, Ctrl-D), connect
   Google Drive again (step 4 above).
2. Download the latest backup:

   ```sh
   sudo RESTIC_REPOSITORY=rclone:gdrive:chat-uses-backups \
        RESTIC_PASSWORD_FILE=/etc/chat-uses/secrets/restic-password \
        RCLONE_CONFIG=/etc/chat-uses/rclone.conf \
        restic restore latest --tag chat-uses --target /root/restore
   ```

3. Settings and secrets: copy `/root/restore/etc/chat-uses/` over
   `/etc/chat-uses/` and `/root/restore/etc/wireguard/` over
   `/etc/wireguard/`, then `sudo infra/host/bootstrap.sh` (all steps).
4. The golden image: `sudo incus image import
   /root/restore/var/lib/chat-uses/backup/images/debian-xfce-*/*.tar
   --alias chat-uses/debian-xfce --project infra`. The others:
   `images/build.sh`, then each `deploy.sh` (docs/phase3.md to phase5.md).
5. brain's store, once `images/brain/deploy.sh` made brain:

   ```sh
   sudo incus exec brain --project infra -- systemctl stop chat-brain
   sudo incus exec brain --project infra -- rm -f /var/lib/chat-brain/brain.db-wal /var/lib/chat-brain/brain.db-shm
   sudo incus file push /root/restore/var/lib/chat-uses/backup/brain.db brain/var/lib/chat-brain/brain.db --project infra
   sudo incus file push /root/restore/var/www/chat-uses/img/*.png brain/var/lib/chat-brain/thumbs/ --project infra
   sudo incus exec brain --project infra -- chown -R brain:brain /var/lib/chat-brain
   sudo incus exec brain --project infra -- systemctl start chat-brain
   ```

6. `sudo rm -rf /root/restore`.

### Tests

`infra/host/test_backup.py` (in CI with the other host tools), against a
fake `incus` and a fake `restic`: a whole backup (brain's copy, the image,
the secrets but restic's password, the site's pictures but not its pages,
the deletion of old backups), the same build not exported twice, a new one
replacing the old export, brain down (the rest still backed up, its stale
copy dropped), a missing image or repository, `--check`, a drill passing
and a damaged backup failing one; and what the monitor tells `#ops`.
`control/tests/test_store.py`: brain's copy, with YouTube's data expired
ahead and nothing left in the file's free pages; `tests/test_brain.py`:
`chat-brain-admin backup`.
