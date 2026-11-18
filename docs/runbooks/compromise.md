# Suspected compromise

Signs that someone has more than chat's access: an SSH login or a WireGuard
peer you don't know (`last`, `sudo journalctl -u ssh`, `sudo wg show`),
processes on the host you didn't start, the target reaching places it
can't (egress refusing many private addresses: its log, below), the bot
posting what brain didn't send, a secret used where it shouldn't be (a
Google or Discord security email).

The order: **stop it, keep the evidence, change every key, rebuild clean.**

## 1. Stop it

1. Panic (`/panic` or `admin panic`): the stream is covered, the machine
   muted, input frozen.
2. Keep a copy of the target as it is, then stop it:
   ```sh
   sudo incus snapshot create chatpc forensic-$(date -u +%Y%m%d-%H%M) --project targets
   sudo incus stop chatpc --project targets --force
   ```
3. If it's **the host** that's not yours any more, don't trust anything on
   it, this runbook's commands included: shut it down from netcup's
   customer control panel and go to step 4 from another computer.

## 2. Keep the evidence

On the host, if it's still trusted, into a folder only root reads:

```sh
sudo install -d -m 0700 /root/incident
sudo incus file pull egress/var/log/squid/access.log /root/incident/ --project infra   # 14 days of the target's web requests
sudo sh -c 'journalctl -u ssh --since -14d >/root/incident/ssh.log'
sudo sh -c 'wg show >/root/incident/wireguard.txt; last -n 50 >/root/incident/logins.txt'
```

brain's audit log (who sent what, who did what) stays in brain's store:
the dashboard shows it.

## 3. Change every key

Each one, in the place it's made; put the new ones on the server the usual
way (`sudo sh -c 'umask 077; cat >/etc/chat-uses/secrets/NAME'`, paste,
Enter, Ctrl-D), then redeploy what uses it.

| Secret | Where to change it | Then |
|---|---|---|
| Discord bot token | Discord Developer Portal → the app → Bot → Reset Token | `discord-bot-token`; `images/brain/deploy.sh` |
| YouTube Data API key | Google Cloud console → APIs & Services → Credentials: a new key restricted to the YouTube Data API, delete the old | `google-api-key`; `images/brain/deploy.sh` |
| YouTube stream key | YouTube Studio → Go live → Stream: a new stream key, the old one deleted | `youtube-stream-key`; `images/bcast/deploy.sh` |
| The dashboard's token | `sudo rm /etc/chat-uses/secrets/brain-admin-token` | `images/brain/deploy.sh` makes a new one |
| `#ops` webhook | Discord: `#ops` → Integrations → Webhooks: delete it, make a new one | `ops-webhook`; `chat-uses-monitor --test` |
| brain's Incus certificate | `sudo rm /etc/chat-uses/secrets/brain-incus.*` | `infra/host/bootstrap.sh incus-api` (makes and trusts a new one, drops the old), `images/brain/deploy.sh` |
| Google Drive (rclone) | myaccount.google.com → Security → third-party access: remove rclone | `rclone config reconnect gdrive:` (docs/phase10.md step 3) |
| restic's password | `sudo RESTIC_REPOSITORY=rclone:gdrive:chat-uses-backups RESTIC_PASSWORD_FILE=/etc/chat-uses/secrets/restic-password RCLONE_CONFIG=/etc/chat-uses/rclone.conf restic key passwd` | the new one in your password manager and in `restic-password` |
| WireGuard | `sudo rm /etc/wireguard/chat-uses-server.* /etc/wireguard/chat-uses-peers/*`, `sudo infra/host/bootstrap.sh wireguard`: from SSH on the public address or netcup's console, not over the tunnel being replaced | `infra/host/wg-add-peer.sh NAME` for each device, and import its new config |
| SSH | new keys on your computers; check `~/.ssh/authorized_keys` for strangers | |
| Your accounts | Discord, Google, netcup: passwords and 2FA, sessions signed out | |

## 4. Rebuild clean

If the host may have been reached, rebuild rather than clean: a fresh
Debian from netcup, `bootstrap.sh` (`docs/phase1.md`), then the restore in
`docs/phase10.md` ("Restoring for real"). Pick a backup from **before** the
compromise (`restic snapshots`, then `restic restore ID` instead of
`latest`): they go back 7 days. Use the **new** secrets, not the ones in
the backup.

## 5. Tell

- Discord `#announcements`: the stream is paused for maintenance.
- **Personal data reached by someone else** (the store holds viewers' IDs,
  names and commands): within 72 hours, notify the data protection
  authority (France: CNIL, notifications.cnil.fr), and the people
  concerned if it puts them at risk (GDPR articles 33 and 34). Keep a note
  of what happened either way.
- The post-mortem in `docs/incidents/` once it's over.
