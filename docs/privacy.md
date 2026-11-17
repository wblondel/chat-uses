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
| YouTube data 30 days at most (YouTube API Services Developer Policies III.E.4: delete or refresh after 30 days) | the same daily pass, `Store.expire_youtube` (D75): a YouTube viewer YouTube hasn't given us again for 30 days (`users.refreshed`, set by each message) is forgotten as for a deletion request, points included; older YouTube commands, events, credits and proposers go; dead runs' pages say "a YouTube viewer"; sessions lose YouTube's viewer counts. Logged as `youtube-expiry`, counts only |
| A YouTube ban keeps working | its channel ID (nameless) is checked with YouTube every 20 days (`channels.list`, 1 unit for 50: `Brain.refresh_youtube_bans`), which refreshes it; a channel that's gone is dropped. If YouTube can't answer for 30 days, the row stays nameless with the ban |
| No YouTube statistics kept | brain no longer reads YouTube's concurrent viewers (session reports count chat only; YouTube Studio has the viewers) |
| `#graveyard` posts name no YouTube viewer | a post can't be taken back after 30 days: "Probably killed by a YouTube viewer" (`Brain._graveyard`) |
| Deletion on request, within 7 days | `sudo incus exec brain --project infra -- chat-brain-admin forget WHO` (a name as the overlay showed it, or the ID); it reports what it deleted and logs the request without the ID. YouTube's rules ask for 7 days: do it when asked |
| Players see the terms and privacy notice first | the stream's notice, bottom right ("Playing means accepting the rules and privacy notice: SITE/rules.html", from `SITE_URL`); the site's footer links the rules, the privacy notice and YouTube's Terms of Service; put the paragraph from docs/rules.md in the YouTube description |
| Ordinary chat not stored | the pipeline stores only `!` commands (`pipeline.py`) |
| Names with slurs or personal details hidden | `TextFilter.safe_name` (D43) |
| Moderators only | the dashboard needs its token over WireGuard; `#mod-alerts` is a private channel |
| The site keeps no visitor log | Caddy's config (`infra/host/Caddyfile.tmpl`) has no `log` directive, so no access log; no cookies, no scripts (its Content-Security-Policy forbids them) |
| Behind Cloudflare, still no scripts | the contact address is wrapped in `<!--email_off-->` (`site.inline`), which Cloudflare's Email Address Obfuscation leaves alone, so the obfuscation can stay on for the zone: otherwise it hides the address behind a script the CSP blocks. Other Cloudflare features that add scripts (Rocket Loader, Web Analytics' automatic setup) are blocked by the CSP too: harmless, but they never run |
| Names on the site are the stream's | the site is rendered from the store with `TextFilter.safe_name` (`control/src/chatuses/site.py`) |
| A deletion request reaches the site | brain renders the site again after `forget`; the host's copy follows within a minute (`infra/host/site_sync.py` deletes what brain no longer lists) |

Backups: if you back up `brain.db` (Phase 8), keep the backups no longer than
30 days (YouTube's data) or the retention promises don't hold for them.

Old session reports: reports saved before D75 (`/var/lib/chat-uses/sessions/`
on the host, and their posts in `#ops`) may show YouTube's viewer counts.
Delete those lines, or the files, once they're 30 days old.
