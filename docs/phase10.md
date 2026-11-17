# Phase 10 — Growth and 24/7 operations

PLAN.md Phase 10. Done when the stream runs 7 days 24/7 without manual
intervention. Built so far, in this order:

1. **The host's monitor: alerts in `#ops`** (below).

Still to build: night mode (PLAN.md 7.6), backups to Google Drive with a
restore drill, event clips, VOD rotation, the timelapse.

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
