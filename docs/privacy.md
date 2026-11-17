# Privacy notice (draft)

A draft of the notice PLAN.md section 11 asks for, written from what the
code really stores and deletes. Not legal advice: have it read before
publishing, fill in the placeholders, and link it from the YouTube
description and Discord `#rules`.

---

The notice itself is **`site/pages/privacy.md`**: the public site publishes
it as `privacy.html` (Phase 8), and `chat-brain-admin site` lists the
placeholders still in it. Change it there, then rebuild brain's image
(`images/build.sh brain && images/brain/deploy.sh --replace`).

---

## For the operator: how each promise is kept

| Promise | How |
|---|---|
| 90 days for commands, logs, screenshots | brain's retention pass, daily (`Store.prune`, screenshots by date); the event log's trigger refuses deleting anything younger, except for a deletion request |
| Deletion on request | `sudo incus exec brain --project infra -- chat-brain-admin forget WHO` (a name as the overlay showed it, or the ID); it reports what it deleted and logs the request without the ID |
| Ordinary chat not stored | the pipeline stores only `!` commands (`pipeline.py`) |
| Names with slurs or personal details hidden | `TextFilter.safe_name` (D43) |
| Moderators only | the dashboard needs its token over WireGuard; `#mod-alerts` is a private channel |
| The site keeps no visitor log | Caddy's config (`infra/host/Caddyfile.tmpl`) has no `log` directive, so no access log; no cookies, no scripts (its Content-Security-Policy forbids them) |
| Names on the site are the stream's | the site is rendered from the store with `TextFilter.safe_name` (`control/src/chatuses/site.py`) |
| A deletion request reaches the site | brain renders the site again after `forget`; the host's copy follows within a minute (`infra/host/site_sync.py` deletes what brain no longer lists) |

Backups: if you back up `brain.db` (Phase 8), keep the backups no longer than
90 days, or the retention promise doesn't hold for them.
