# Incident runbooks

What to do when something goes wrong live (PLAN.md 10.2). Each page is a
list to follow in order, for the operator and the moderators. Before every
session, `sudo infra/host/golive.sh` (PLAN.md 10.1) checks what can be
checked.

| Something's wrong | Page | Who |
|---|---|---|
| Something on screen, in the log or in chat that mustn't be there | [content-incident.md](content-incident.md) | any moderator |
| A crowd of new accounts flooding chat, votes or the screen | [raid.md](raid.md) | any moderator |
| The stream is down, frozen or silent on YouTube | [stream-down.md](stream-down.md) | the operator |
| "Controls offline" on stream, chat commands do nothing | [brain-down.md](brain-down.md) | the operator |
| The server may be in someone else's hands | [compromise.md](compromise.md) | the operator |
| netcup forwards an abuse report | [abuse-report.md](abuse-report.md) | the operator |

**The first move is the same for all of them:** if viewers could see or hear
something they shouldn't, **panic first**, think after. Panic covers the
stream in under a second, mutes the machine and freezes input; it costs
nothing, and `/resume` undoes it.

| Panic from | |
|---|---|
| Discord | `/panic [reason]` (moderators) |
| YouTube or Discord chat | `!panic [reason]` (moderators and the owner) |
| The dashboard | **PANIC** (`http://10.20.0.11:8080/admin`, over WireGuard) |
| The server | `admin panic [reason]` |

On the server, every page uses this shortcut (type it once per shell):

```sh
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
```

**Never paste secrets or raw logs anywhere public.** The forwarder's
journal holds the YouTube stream key (read it masked: stream-down.md), and
brain's journal and the audit log hold viewers' IDs.

**After any incident that reached viewers**, write a short post-mortem in
`docs/incidents/` from [`docs/incidents/TEMPLATE.md`](../incidents/TEMPLATE.md):
what happened, what was done, what changes so it doesn't happen again. No
viewer's name or ID in it (say "a YouTube viewer", "a Discord member").
