# Privacy notice (draft)

A draft of the notice PLAN.md section 11 asks for, written from what the
code really stores and deletes. Not legal advice: have it read before
publishing, fill in the placeholders, and publish it on the site (Phase 7)
and link it from the YouTube description and Discord `#rules`.

---

## Privacy notice for chat-uses

**Who:** chat-uses is run by [OPERATOR NAME], [COUNTRY]. Contact:
[CONTACT ADDRESS].

**What this is:** a live stream where chat controls a computer. To run it,
we process what you send in the stream's chat (YouTube) and in our Discord
server's `#control` channel.

### What we keep

- **Who you are on the platform:** your YouTube channel ID or Discord user
  ID, your display name as the platform shows it, when we first and last saw
  you send a command, and your roles there (moderator, member, regular).
- **Your commands:** each `!` command you send, when, and what became of it
  (run, refused, and why). Pressing a button on the Discord control panel
  counts as sending its command. Ordinary chat messages aren't stored.
- **Moderation records:** timeouts and bans (with who decided and why), and
  the moderation log (panics, lockdowns).
- **Screenshots of the stream's computer** when its text matched our list of
  slurs (screen OCR), for moderators to check.
- **Session statistics:** for each stream session, counts only (how many
  watched, how many were active in chat, how many commands and votes),
  never who. These are kept after the 90 days.

We don't collect anything the platforms don't already show in chat: no
email, no IP address, no payment data.

### Why

To run the game (who sent which command, rate limits, votes and points),
and to keep the stream safe (filtering, timeouts and bans, investigating
incidents). Legal basis: our legitimate interest in running the stream and
keeping it safe (GDPR art. 6(1)(f)).

### How long

- **Commands, the event log and screenshots:** 90 days, then deleted
  automatically.
- **Your platform ID, name and roles:** while you take part; leaderboards
  and other aggregates stay until you ask us to delete them.
- **Bans:** as long as the ban lasts.

### Who sees it

Our moderators, through private tools. What is public is what was public
already: the stream shows commands and display names as chat sent them
(names with slurs or personal details are shown as `viewer#1234`). Our
server runs at netcup GmbH (data centre: [LOCATION]). YouTube and Discord process your data
under their own policies.

### Your rights

You can ask to see, correct or delete your data, or object to its use:
write to [CONTACT ADDRESS] or ask a moderator on Discord, with your channel
or user ID. We delete your commands and log entries within 30 days (a ban
in force is kept, without your name, so it can't be avoided this way). You
can also complain to your data protection authority.

---

## For the operator: how each promise is kept

| Promise | How |
|---|---|
| 90 days for commands, logs, screenshots | brain's retention pass, daily (`Store.prune`, screenshots by date); the event log's trigger refuses deleting anything younger, except for a deletion request |
| Deletion on request | `sudo incus exec brain --project infra -- chat-brain-admin forget WHO` (a name as the overlay showed it, or the ID); it reports what it deleted and logs the request without the ID |
| Ordinary chat not stored | the pipeline stores only `!` commands (`pipeline.py`) |
| Names with slurs or personal details hidden | `TextFilter.safe_name` (D43) |
| Moderators only | the dashboard needs its token over WireGuard; `#mod-alerts` is a private channel |

Backups: if you back up `brain.db` (Phase 8), keep the backups no longer than
90 days, or the retention promise doesn't hold for them.
