# Phase 0 — Groundwork (runbook)

Accounts, server check and Discord setup. Most of this happens in web UIs, so
it's a checklist; the repo provides two helpers:

- `infra/host/server-check.sh`: what the server is, and how much CPU your
  neighbours steal under load.
- `infra/discord/setup_server.py`: creates the Discord roles and channels.

UI labels move around; if a menu below has a slightly different name, look
for the closest match.

**Done when** (from PLAN.md): you can get a YouTube stream key, the Discord
bot is in your server with its channels, and the server-check summary is in
the decision log (PLAN.md section 15).

---

## 1. Secure your accounts first (15 min)

- [ ] Password manager ready; every credential below goes in it.
- [ ] 2FA on: Google account, Discord, GitHub, netcup customer control panel (CCP).
- [ ] netcup: note how to open the **web console** of your server (Server Control
      Panel → your server → screen/console). It's your way back in if SSH ever
      locks you out, and it logs in with root's **password**, so make sure you
      know it (`passwd` as root if not).

## 2. Check the server (15 min)

On the server, as root:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/wblondel/chat-uses /opt/chat-uses
/opt/chat-uses/infra/host/server-check.sh            # ~11 minutes
```

If the repository is private, either add a read-only
[deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
or copy it from your laptop (`rsync -a chat-uses/ root@SERVER:/opt/chat-uses/`).

Read the result:

| Line | What you want | If not |
|---|---|---|
| OS | Debian 13 | Reinstall from the netcup image before Phase 1 |
| `/dev/kvm` | absent | If present, nested virtualization works after all; tell me, it opens options |
| btrfs, wireguard | available | Both ship with Debian's kernel; a custom kernel would need them |
| cgroup | v2 | Debian 13 uses v2; v1 means something unusual |
| Steal, full load (avg) | < 2 % | 2–5 %: OK with CPU pinning. > 5 %: expect stream stutter; consider a Root Server |
| Swap | 0 | Not blocking; Phase 1 warns about it (targets could use swap) |

- [ ] Paste the `== Summary` table row into PLAN.md section 15.
- [ ] Write the server type (VPS or Root Server) into PLAN.md section 13, question 3.

## 3. Name and domain (whenever)

- [ ] Channel name (PLAN.md section 13, question 2).
- [ ] Domain for the public site (Phase 8). Point it at the server later; nothing needed now.

## 4. YouTube (needs up to 24 h of waiting)

- [ ] YouTube Studio → **Create → Go live**. Complete phone verification. Live
      streaming can take up to 24 hours to activate; start this early.
- [ ] Once active: **Stream** tab → stream key → **Create new stream key**:
      name `chat-uses main`, reusable. Copy it into your password manager.
      It goes on the server only in Phase 3, as `/etc/chat-uses/secrets/youtube-stream-key`.
- [ ] Stream settings for your test broadcasts:
  - Stream latency: **Ultra-low latency**
  - Audience: **No, it's not made for kids**
  - Live chat: on
- [ ] Studio → **Settings → Community → Automated filters**: add a blocked-words
      list (same languages as your audience). Leave "hold potentially inappropriate
      messages" on for now; in Phase 4 check it doesn't hold normal `!` commands.

## 5. Google Cloud (20 min)

Needed to read YouTube chat (Phase 4) and later to rotate broadcasts (Phase 10).

- [ ] <https://console.cloud.google.com> → new project `chat-uses`.
- [ ] **APIs & Services → Library → YouTube Data API v3 → Enable.**
- [ ] **Credentials → Create credentials → API key.** Restrict it:
  - API restrictions: YouTube Data API v3 only.
  - Application restrictions: IP addresses → your server's public IPv4.
  - Store it as `google-api-key` in the password manager.
- [ ] **OAuth consent screen** (may be called *Google Auth Platform*):
  - User type **External**, app name `chat-uses`, your email as support/developer contact.
  - Add your Google account as a test user.
  - Then set the publishing status to **In production**. ⚠️ Apps left in
    *Testing* get refresh tokens that expire after 7 days, which would silently
    break the bot every week. An "unverified app" warning is fine for your own use.
- [ ] **Credentials → Create credentials → OAuth client ID**, type **Desktop app**.
      Download the JSON into the password manager (it becomes
      `/etc/chat-uses/secrets/google-oauth-client.json` later).
- [ ] Look at **APIs & Services → YouTube Data API v3 → Quotas** and note the
      daily quota (10,000 units by default).

## 6. Discord (30 min)

### 6.1 Server and application

- [ ] Create the server (**Create My Own → For a club or community**).
- [ ] Server Settings → **Safety Setup**: verification level at least Medium,
      explicit media filter for all members, **require 2FA for moderator actions**.
- [ ] User Settings → **Advanced → Developer Mode** on (lets you copy IDs).
- [ ] <https://discord.com/developers/applications> → **New Application** `Chat Uses`.
      Copy the **Application ID**.
- [ ] **Bot** tab:
  - **Reset Token**, copy it to the password manager (shown once).
  - **Public Bot: off** (only you can add it to servers).
  - **Message Content Intent: on** (needed to read `!` commands in `#control`).

### 6.2 Add the bot and create the channels

On your laptop (or the server), from the repository:

```bash
python3 infra/discord/setup_server.py invite-url --application-id <APPLICATION_ID>
```

- [ ] Open the printed link, pick your server, authorize. The link asks for exactly
      the permissions the bot needs (moderation, posting, roles, the audio stage).

Right-click your server icon → **Copy Server ID**, then:

```bash
read -rs DISCORD_BOT_TOKEN && export DISCORD_BOT_TOKEN   # paste the token; it stays out of shell history
python3 infra/discord/setup_server.py apply --guild-id <SERVER_ID> --dry-run
python3 infra/discord/setup_server.py apply --guild-id <SERVER_ID>
```

This creates:

| Category | Channels | Who can post |
|---|---|---|
| 📢 Info | `#announcements`, `#rules` | Moderators and bot |
| 🕹️ Play | `#control` (2 s slow mode), `#general`, `#theorycraft`, stage **📻 Machine audio** | Everyone (stage: bot speaks) |
| 📜 Records | `#graveyard`, `#achievements`, `#clips` | Bot and moderators |
| 🔒 Staff | `#mod-alerts`, `#ops` | Hidden from everyone but moderators and the bot |

Roles: **Owner** (cosmetic), **Moderator** (kick, ban, timeout, manage messages,
mute/move in voice), **Regular** (cosmetic, granted by the bot later).
Moderation uses Discord's built-in **timeouts**, so there is no "Muted" role.

The stage channel needs a Community server:

- [ ] Server Settings → **Enable Community**. When asked, choose `#rules` as the
      rules channel and `#announcements` for updates.
- [ ] Run `apply` again: it creates only the stage channel.

Then:

- [ ] Give yourself **Owner**, your moderators **Moderator**.
- [ ] Unset the token: `unset DISCORD_BOT_TOKEN`.

Running `apply` again later is safe: it only creates what's missing. Add
`--sync-permissions` to reset permissions of existing roles and channels to the
layout in the script.

## 7. People and decisions

- [ ] Audience language(s) → PLAN.md section 13, question 1 (drives word filters).
- [ ] Ask 2–3 people you trust to moderate the beta.
- [ ] Optional now: answers to the other open questions in PLAN.md section 13.

## Where secrets will live

Nothing secret goes into the repository. On the server, secrets live under
`/etc/chat-uses/secrets/` (mode `0600`, root only), created in the phase that needs them:

| Secret | File on the server | Needed from |
|---|---|---|
| YouTube stream key | `youtube-stream-key` | Phase 3 |
| Google API key | `google-api-key` | Phase 4 |
| Discord bot token | `discord-bot-token` | Phase 4 |
| Google OAuth client | `google-oauth-client.json` | Phase 10 |
