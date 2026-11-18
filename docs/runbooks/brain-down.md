# brain down

The stream shows "Controls offline"; chat commands do nothing; `#ops` gets
the host monitor's "brain doesn't answer". The stream itself goes on: bcast
doesn't need brain (PLAN.md D27). systemd restarts brain within seconds of
a crash, so this page is for when it stays down or keeps falling.

## 1. What it says

```sh
sudo incus exec brain --project infra -- systemctl --no-pager status chat-brain
sudo incus exec brain --project infra -- journalctl -u chat-brain -n 80 --no-pager
```

(The journal holds viewers' IDs: read it, don't paste it anywhere public.)
Look for the last `Traceback` and the line above it.

- **The container is stopped:** `sudo incus start brain --project infra`.
- **A setting or secret it can't read** (`Config`, `brain.local.env`, a
  token): fix it on the host and `sudo images/brain/deploy.sh` (pushes the
  settings and restarts).
- **Its disk or the store's volume is full:** `sudo chat-uses-monitor
  --check`, `sudo incus exec brain --project infra -- df -h /var/lib/chat-brain`.

## 2. It broke after a deploy: roll back

Every build keeps a dated alias. Go back to the last one that worked; the
store is on its own volume and stays:

```sh
sudo incus image alias list --project infra | grep 'chat-uses/brain/'
sudo images/brain/deploy.sh --replace --image chat-uses/brain/YYYYmmdd-HHMM
sudo images/brain/check.sh
```

Then fix it in the code, and deploy again (`images/build.sh brain &&
images/brain/deploy.sh --replace`).

## 3. The store itself is broken

brain fails on its database (`sqlite3.DatabaseError`, "malformed"):
restore last night's copy from the backups, as in `docs/phase10.md`,
"Restoring for real", step 5 (download with `restic restore latest`, then
push `brain.db` into brain). What happened since that backup is lost: the
day's commands and points.

## 4. While it's down

Chat can't play: say so in Discord. Moderation still works where it
doesn't need brain: bans and timeouts on Discord and YouTube themselves,
and `chat-bcast-scene brb` covers the stream
(`sudo incus exec bcast --project infra -- chat-bcast-scene brb "Back soon"`).
