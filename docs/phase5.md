# Phase 5 — Safety layer (runbook)

**The gate: no public stream before this phase is done.** It keeps what chat
does inside the machine (network), off the stream (filters, content hold) and
under the moderators' control (panic, bans, lockdown).

**Done when** (from PLAN.md): all 21 red-team tests pass, panic is < 1 s, and
at least two moderators have practised every tool on an unlisted stream.

| Step | What | State |
|---|---|---|
| 1 | `egress` proxy (allowlist, no private addresses, bandwidth cap) and `intranet` (homepage, files, offline Wikipedia) | done; to deploy on the server |
| 2 | Text filter: `!type` text with a rolling buffer, names on the overlay, strikes and automatic timeouts | done; comes with the next brain deploy |
| 3 | Moderation: panic end to end, cross-platform bans and timeouts, lockdown, moderator slash commands, admin dashboard with audit viewer | done; to set up on the server |
| 4 | Screen OCR content hold, in shadow mode first | done; shadow mode on the server, enforce after tuning |
| 5 | Watchdog, audio limits, the whole red-team suite (`docs/redteam.md`), stream rules and privacy notice, moderator practice | watchdog, audio cap, retention, rules and privacy drafts done; red-team runner next |

## Step 1: egress and intranet

What a target can reach (PLAN.md 7.2), now with the real containers behind
the addresses Phase 1's firewall and Phase 2's desktop already point at:

- **`egress`** (10.20.0.12:3128), Squid: targets may fetch from the domains
  in `images/egress/rootfs/etc/squid/chat-uses-allow.txt` (Debian's package
  archive: `deb.debian.org`, `security.debian.org`), over HTTP or HTTPS
  (CONNECT to port 443 only). Everything else is refused: other sites,
  addresses typed directly, and any private address, so a target can't use
  the proxy to reach brain or anything else on `infranet` (D37). All targets
  share about 2 MB/s (`EGRESS_BYTES_PER_SECOND`). Nothing is cached; the
  access log is kept 14 days.
- **`intranet`** (10.20.0.13:80), nginx: a homepage (Firefox's start page on
  the desktop), a file cabinet with Freedoom's game data, and the offline
  Wikipedia at `/wiki/` (kiwix-serve, reachable only through nginx). Reading
  only: anything but GET and HEAD is refused.

The desktop needs no change: apt, the shell and Firefox already use the
proxy, and Firefox only opens the intranet (Phase 2).

### 1. Update and build

```bash
cd /opt/chat-uses && git pull
sudo images/build.sh egress
sudo images/build.sh intranet
```

### 2. Deploy the proxy and check it

```bash
sudo images/egress/deploy.sh
sudo images/egress/check.sh
```

The check runs from inside a target (by default the first running one, e.g.
`phase2-check`): the Debian archive works (HTTP, HTTPS, `apt-get update`);
`example.com`, `1.1.1.1`, brain's feed, the host, other ports are refused;
a download stays under the cap; without the proxy nothing gets out.

### 3. Pick an offline Wikipedia and deploy the intranet

Open <https://download.kiwix.org/zim/wikipedia/> and pick a **`nopic`** file
(words only: pictures could put anything on stream), the most recent date:

- `wikipedia_en_simple_all_nopic_YYYY-MM.zim`: Simple English, small. A good
  start.
- `wikipedia_en_top_nopic_…` or `wikipedia_fr_all_nopic_…`: bigger. The
  listing shows each size; it must fit in 20 GiB (`DISK_INTRANET`).

```bash
sudo images/intranet/deploy.sh --zim https://download.kiwix.org/zim/wikipedia/wikipedia_en_simple_all_nopic_YYYY-MM.zim
sudo images/intranet/check.sh
```

The download happens inside the container and is checked against Kiwix's
`.sha256`. The file lives on its own volume (`intranet-zim`), so redeploying
with `--replace` keeps it; `--zim` again swaps it for another.

### 4. Look at it on the desktop

On the stream (or over VNC), open Firefox on the desktop: the homepage is
the intranet. `!key ctrl+alt+t`, then `!type sudo apt install cowsay` and
`!enter` installs through the proxy; `curl https://example.com` is refused.

### The isolation test

`infra/tests/net-isolation.sh` needs the egress and intranet addresses for
its own stand-ins. It now stops the real containers for the test and starts
them again afterwards (outside stream hours: the desktop loses its proxy and
intranet for a minute).

### Changing what targets can reach

Every allowed domain is a decision for PLAN.md's log: live Wikipedia,
Wikimedia Commons or GitHub would let chat put any picture or file on stream.
To add one: edit `images/egress/rootfs/etc/squid/chat-uses-allow.txt`, add a
decision to PLAN.md section 15 saying why it's safe, then
`sudo images/build.sh egress && sudo images/egress/deploy.sh --replace`.

## Step 2: the text filter

brain now refuses what shouldn't reach the stream in words (PLAN.md 7.4, D43):

- **Slurs and hate speech** in English, French, Spanish, Italian and Arabic,
  plus hate codes (`control/src/chatuses/safety/lists/`). General swearing
  is allowed.
- **Personal details:** email addresses, phone numbers, street addresses,
  links.

It looks at text typed on the desktop (`!type`), save names, and the names
shown on the overlay. Writing tricks don't get through:
- case, accents and invisible characters;
- styled and fullwidth letters, and look-alike letters from other alphabets;
- leetspeak (`4` and `@` for `a`, `3` for `e`, `1`, `!` and `|` for `i`...);
- repeated letters, and spelling a word out with dots or spaces.

Typed text is checked together with the last 200 characters typed (with
backspaces applied), so a word typed in pieces, by one person or several,
is refused when it would complete.

On stream:
- **A refused command** shows only "that's not allowed on stream" (or the
  personal-details reason), never the text.
- **Strikes:** each refused slur is a strike. Three in 10 minutes earn a
  10-minute timeout, which the overlay shows. The person's commands are
  then ignored until it ends. Personal details are refused without a strike,
  since they're often a mistake.
- **Names:** a name holding a listed word or personal details is shown as
  `viewer#1234` (always the same number for the same person).

### On the server

It's part of brain: rebuild and redeploy it (with the other Phase 4 steps
if you haven't yet).

```bash
cd /opt/chat-uses && git pull
sudo images/build.sh brain
sudo images/brain/deploy.sh --replace
sudo images/brain/check.sh
```

Try it with console chat (`chat-brain-say`, step 3 of `docs/phase4.md`): a
phone number, a word in pieces from two `--as` names, a name with a listed
word.

### The word lists

`control/src/chatuses/safety/lists/` holds one file per language. Their
`README.md` explains the syntax (whole words, `*` for anywhere, phrases,
`!` for ordinary words a `*` term would hit). They are a starting point:
- **Before going public,** have native speakers review them, the Arabic one
  first.
- **Over time,** add what your chat actually tries.

After a change, run the tests: every term is checked against the tricks
above and against ordinary words. Then rebuild and redeploy brain.

```bash
cd control && uv run pytest tests/test_textfilter.py
```

### Not covered yet

- **Text that appears on screen without being typed** (`echo <base64> |
  base64 -d`, a downloaded file, a renamed window): step 4's screen OCR.
- ~~Lifting a timeout early, or banning by hand~~: step 3's moderator
  commands (`/unban`, `/ban`).

## Step 3: moderation

The moderators' tools (PLAN.md 7.5, 7.6, D44, D45). Every one works from
four places, does the same thing from each, and goes in the log with who
did it:

| | Discord (Moderator, Owner) | Admin dashboard | Chat (moderators) | `chat-brain-admin` |
|---|---|---|---|---|
| Panic | `/panic [reason]` | **PANIC** | `!panic [reason]` | `panic [REASON]` |
| End it | `/resume` | Resume | `!resume` | `resume` |
| Ban | `/ban who duration [reason]` | Ban | | `ban WHO DURATION [REASON]` |
| Time out | `/timeout who [minutes]` | Ban, `10m` | | `timeout WHO [MINUTES]` |
| Lift a ban | `/unban who` | Unban | | `unban WHO` |
| Raid | `/lockdown on\|off` | Lockdown | | `lockdown on\|off` |
| Look | `/status` | Audit, log | | `status`, `bans` |

`!panic` and `!resume` in chat are for YouTube's moderators, who have no
slash commands: brain takes them from anyone YouTube (or Discord) marks as a
moderator or the owner. From anyone else, they're an unknown command.

### Panic

Within a second of the request:

1. **The stream is covered:** "We'll be right back", over everything. The
   layout page gets it straight from brain's feed.
2. **The target's sound is muted** in the mix: `chat-bcast-panic` in bcast
   follows the same feed.
3. **Input is frozen:** the command running stops where it is (every key
   and button let go), the waiting ones are dropped, new ones are refused
   ("the machine is paused").
4. **A private forensic snapshot** of the target, `panic-YYYYMMDD-HHMMSS`
   (in UTC), through the Incus API. Chat can't rewind to it.
5. **A post in `#mod-alerts`**, then one with the snapshot's result.

It lasts until someone resumes: restarting brain or bcast doesn't end it,
and while bcast can't reach brain it stays as it was. Without brain at all,
`sudo incus exec bcast --project infra -- chat-bcast-scene brb` still
covers the stream and mutes the target (`live` to end it).

### Bans, timeouts, lockdown

- **Who:** the name as the overlay showed it (`viewer#1234` too), for anyone
  who sent a command in the last 7 days; or their ID, `yt:UC…` or `dc:…`
  (the dashboard's audit shows it); or a Discord mention. Two people with the
  same name: use the ID. A ban by ID works before the person shows up.
- **Bans and timeouts** stop someone's commands on every platform, until the
  end you set (`30m`, `2h`, `7d`, `1w`, `perm`); a moderator can shorten one
  or lift it, which also clears their strikes. Moderators and the owner can't
  be banned this way. To mute someone in Discord itself, use Discord's own
  timeout (D15).
- **Lockdown** (a raid): only moderators, the owner, Discord's Regulars,
  YouTube channel members, and people brain has known for a day can send
  commands, until it's lifted. The overlay shows "Regulars only";
  the commands it refuses aren't listed one by one (a raid would fill the
  log).

### The admin dashboard

`http://10.20.0.11:8080/admin`, over the WireGuard tunnel only (laptop: see
`docs/phase1.md`). It asks for a token, kept in that browser tab:

```bash
sudo cat /etc/chat-uses/secrets/brain-admin-token
```

It shows panic and lockdown, the stream's status, who is banned and until
when, the **audit** (every command: when, who, their ID, what they sent,
what became of it; click a name to see only theirs), and the moderation log
(panics, bans, lockdowns, snapshots). The name you type at login goes in
the log as who did it. Anyone with the token can use it: share it only with
moderators who need it (the others have Discord), and to change it, delete
the file and run `images/brain/deploy.sh` again.

A hotkey or a Stream Deck button can call the same API:

```bash
curl -fsS -X POST -H "Authorization: Bearer $(sudo cat /etc/chat-uses/secrets/brain-admin-token)" \
  -H "X-Moderator: Stream Deck" http://10.20.0.11:8080/api/panic
```

### On the server

```bash
cd /opt/chat-uses && git pull
sudo infra/host/bootstrap.sh incus-api   # once: brain's access for the snapshots
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo images/build.sh bcast && sudo images/bcast/deploy.sh --replace   # the stream stops for a minute
sudo images/brain/check.sh
sudo images/bcast/check.sh
```

`incus-api` makes the Incus API listen on infranet's address only
(`10.20.0.1:8443`; the firewall lets only brain in), and trusts a
certificate for brain that reaches only the `targets` project. The API
comes up about 30 s after Incus starts, once the network exists. brain's
deploy pushes the certificate and makes the dashboard's token.

The new slash commands appear in Discord when the bot reconnects; Discord
shows them only to members who can time people out (the Moderator role
can). brain checks the role again each time.

Then practise, on an unlisted stream:

```bash
sudo images/brain/panic-drill.sh
```

It panics for real for a few seconds and measures on the stream itself: the
time until a frame is covered and until the sound is muted (both must be
under 1 s, red-team test 16). It checks that input is refused, the snapshot
and the alert, then resumes and checks the picture and the sound come back.
The drill's snapshot is deleted afterwards (`--keep-snapshot` keeps it).

Forensic snapshots stay until you delete them:

```bash
sudo incus snapshot list phase2-check --project targets
sudo incus snapshot delete phase2-check panic-20261116-201500 --project targets
```

### Not covered yet

- `/rewind-to`, `/freeze`, `/mode`, `/say`, and a panic cancelling open
  votes: they need Phase 6's votes and snapshots.
- Night mode (stricter laws when no moderator is online): the 24/7 phase.
- Mirroring bans to YouTube's own ban: YouTube moderators ban there
  themselves; brain's ban covers commands everywhere.
- Moderator practice: yours (PLAN.md: at least two moderators practise every
  tool on an unlisted stream).

## Step 4: screen OCR

Text can reach the screen without going through `!type`: a decoded file
(`echo <base64> | base64 -d`), a web page, a file or window name made in
pieces. brain now reads the target's screen every 5 seconds and looks the
text up in the slur lists (PLAN.md 7.4, D46).

- **How:** a second VNC connection that only reads the screen (raw pixels,
  every size and rectangle checked: the target is hostile), and tesseract
  for the text. A screen that didn't change isn't read again. Lines are
  checked one by one and each with the next, since a terminal wraps a long
  line anywhere. Personal details aren't looked for: a desktop is full of
  links and numbers.
- **Shadow mode** (the default): a hit goes in the log and in `#mod-alerts`,
  with the screenshot attached as a spoiler and the line blurred; nothing
  happens on stream. Run it like this through the closed beta, and look at
  what it reports: real hits, and false ones (OCR misreads a word into a
  listed one).
- **Enforce mode:** a hit is a **content hold**: like a panic (sound muted,
  input frozen, a forensic snapshot, an alert), but the cover says "Content
  check". A moderator `/resume`s if what's on screen is fine, and the lines
  it saw then won't hold the stream again while they stay on screen; or
  `/panic`s if it isn't, which turns the hold into a panic. Until Phase 6's
  rewind, a hold waits for a moderator: enforce only when one is online.
- **Screenshots** of hits stay in brain (`/var/lib/chat-brain/screens`, the
  last 200); the dashboard's moderation log shows each hit, blurred until you
  point at it, with its screenshot.

### On the server

```bash
cd /opt/chat-uses && git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo images/build.sh bcast && sudo images/bcast/deploy.sh --replace   # the "Content check" cover
sudo images/brain/check.sh
```

The check waits for the first read of the screen and shows how long a read
takes. Settings, in `/etc/chat-uses/brain.local.env` (then
`images/brain/deploy.sh`):

```bash
OCR_MODE=shadow        # shadow, enforce or off
OCR_INTERVAL=5         # seconds between reads
OCR_LANGS=eng+ara      # tesseract's languages
```

`eng` reads French, Spanish and Italian words well enough for the lists,
which ignore accents; `ara` reads Arabic. Each language makes a read slower
(in the sandbox, one core: `eng` 0.34 s, `eng+ara` 0.6 s, all five 1 s):
the check shows the time; keep it well under `OCR_INTERVAL`.

### Trying it

On an unlisted stream, with a moderator watching `#mod-alerts`: open a
terminal on the desktop and make a listed word appear without typing it
(`!type echo <the word in base64> | base64 -d`, `!enter`). Within a few
seconds `#mod-alerts` gets the alert with the screenshot. In enforce mode
the stream says "Content check" until `/resume`.

### Not covered yet

- **Rewind** from a hold, automatically after 3 minutes or by a moderator
  (PLAN.md 7.4): Phase 6's snapshots. The hold's forensic snapshot is
  already taken (`hold-…`).
- **Pictures:** OCR reads text, not images. The egress allowlist keeps
  pictures out (step 1); moderators and panic cover the rest.
- **Audio** (text-to-speech): later, optional (PLAN.md 7.4).

## Step 5: watchdog, sound, privacy, rules

### The watchdog

brain keeps an eye on the target (PLAN.md 6.7, D48), every 10 seconds:

- **The instance runs.** If it's stopped, `#ops` hears it once; brain
  doesn't start it (you may have stopped it; what a dead target means for
  the game comes with Phase 6).
- **The desktop's services** (`chat-xvnc`, `chat-session`, `chat-audio`,
  `chat-audio-pcm`) are active, asked every 30 s through Incus. One that
  isn't is restarted, and `#ops` hears it. At most 3 restarts in 15 minutes
  per service; after that it's left to you, with one alert.
- **VNC:** brain's link down for more than a minute while the services say
  they're fine: the display and the session are restarted.
- **A frozen screen:** commands reached the target and the screen didn't
  change in the 2 minutes after. `#ops` hears it; nothing is restarted (a
  still screen can be fine). This needs screen OCR on, which reads the
  screen.

`chat-brain-admin status`, the dashboard and `images/brain/check.sh` show
what it sees. It uses brain's Incus access from step 3; without it, only the
VNC and screen checks run, and only alert.

### Sound

The encoder now compresses the whole mix above `AUDIO_COMPRESS` (0.2,
−14 dB) at 20:1, in front of the limiter (D47). Measured with ffmpeg's
loudness meter:

| Sound | Before | After |
|---|---|---|
| Full-scale white noise (red-team test 13) | −3.5 LUFS | −14.1 LUFS |
| Pink noise at a normal level | −23.8 | −23.8 |
| Loud bass-heavy noise | −20.0 | −22.1 |
| Very loud pink noise | −16.5 | −20.2 |

−14 LUFS is YouTube's reference loudness: chat's worst noise is as loud as a
loud video, not ear-splitting; normal sound is untouched. To change it, set
`AUDIO_COMPRESS` in `/etc/chat-uses/bcast.local.env` (1 turns it off) and
run `images/bcast/deploy.sh`.

### Privacy: retention and deletion requests

What PLAN.md section 11 promises, brain now does (D49):

- **Commands, the event log and OCR screenshots are deleted after 90 days**,
  by a daily pass. The event log still can't be changed, and nothing younger
  than 90 days can be deleted, except for a deletion request.
- **A deletion request:**
  ```bash
  sudo incus exec brain --project infra -- chat-brain-admin forget WHO
  ```
  WHO is the name as the overlay showed it, or the ID (`yt:…`, `dc:…`). It
  deletes their commands and every log entry naming them, and the person,
  unless they're banned (the ban stays, nameless). The log keeps a line
  saying a request was handled, without the ID.

### Rules and privacy notice

Drafts to make yours before going public:

- `docs/rules.md`: the one-liner for `!rules`, the YouTube description, and
  Discord's `#rules`.
- `docs/privacy.md`: the privacy notice, with placeholders (your name,
  contact address, the data centre). It says what the code does; the table
  at the end shows how each promise is kept. Not legal advice: have it read.

### On the server

```bash
cd /opt/chat-uses && git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
sudo images/build.sh bcast && sudo images/bcast/deploy.sh --replace   # the sound cap
sudo images/brain/check.sh   # now with the watchdog's view
```

Discord: the watchdog posts in `#ops` (`DISCORD_OPS_CHANNEL`), which
`setup_server.py` already made.

## What was tested before this landed

Step 4:

- **Unit tests, 1,958 in all:**
  - the screen reader against a fake VNC server: a whole screen in the
    format asked for, partial rectangles, other messages in between; and a
    hostile server refused (another encoding, a rectangle outside the screen,
    a huge one, an unknown message, a clipboard of 1 GB, a frame cut short,
    a screen too big);
  - PPM and PNG written by hand, checked byte for byte;
  - the watch: words alone and run on across two lines, an unchanged screen
    not read again, a line reported again only after 10 minutes, cleared
    lines quiet while they stay on screen, reading the real (fake) screen,
    pausing during a panic, reconnecting;
  - tesseract's wrapper with a stand-in: the frame on stdin, one thread,
    failures and timeouts (the whole process group killed);
  - brain: shadow mode only reports, enforce mode holds until a moderator
    resumes and then clears the lines, a panic during a hold, settings;
    the dashboard's screenshots behind the token and a strict name; alerts
    with the screenshot attached as a spoiler.
- **In the sandbox**, brain built from this branch (tesseract with English,
  French, Spanish, Italian and Arabic):
  - `images/brain/check.sh`: 15 of 15, OCR reading the screen.
  - **Red-team test 8, the OCR part:** a listed word shown in a terminal with
    `echo <base64> | base64 -d`, typed in commands the text filter accepts,
    was caught **7 s** after Enter, with the screenshot kept.
  - **Enforce mode:** the stream showed "Content check" within 7 s of brain
    starting with the word on screen, the sound was muted and input paused;
    after `resume` the same text didn't hold the stream again.
  - **False positives:** 16 reads of ordinary screens (`ls`, `dpkg -l`,
    `/etc/services`, `ps`, `df`, `ip a`, `apt-cache search`, `man bash`, the
    GPL, Firefox on the intranet's Wikipedia): none.
  - **Speed** of one read (one core): `eng` 0.34 s, `eng+ara` 0.6 s, all five
    languages 1 s. `eng+ara` read a few stray Arabic letters off a Latin page,
    nothing listed.
  - **The dashboard:** OCR hits blurred until pointed at, the screenshot
    opening from the log.
- **Found and fixed while testing:**
  - **A tesseract that timed out left its pipes open:** its child processes
    kept them. It now runs in its own process group, killed whole.
  - **`!type` takes 40 characters,** so the red-team command is typed short:
    a real attack would be too.

Step 3:

- **Unit tests, 1,933 in all:**
  - moderation: durations, finding people by name, shown name, ID and
    mention; refusals (moderators, two people with one name, unknown
    names, bad durations); shortening and lifting bans; strikes forgotten;
    lockdown for each role and for known and new people; a panic and a
    lockdown kept across a restart;
  - the executor stopping a drag half-way with the button let go, dropping
    the queue, refusing until it thaws, and stopping cleanly when brain
    stops;
  - brain: the feed's `panic` message before anything else, paused input
    kept off the overlay, a page connecting during a panic covered at once,
    moderators' `!panic`/`!resume` in chat (and nobody else's), every
    `chat-brain-admin` verb, snapshots taken and failing;
  - the dashboard's API: every call refused without the right token, the
    page served without any secret and with its security headers, actions,
    bad requests, the audit's paging and filters;
  - Discord: the commands' visibility and role check (someone else, the
    owner of another server, a moderator), private answers, alerts kept
    while Discord is unreachable and posted with mentions off;
  - the Incus client over real TLS: the pinned certificate accepted, an
    impostor's refused, a client certificate required.
- **In the sandbox**, brain and bcast built from this branch and deployed:
  - `bootstrap.sh incus-api` set up the API on `10.20.0.1:8443` and the
    restricted certificate; run again, it changed nothing. After a restart
    of Incus, the API came back once the bridge existed (30 s).
  - **brain's certificate:** sees only project `targets`; asking for an
    instance in `infra` is refused (403). It snapshotted a running
    container on btrfs in 0.24 s. The sandbox's targets sit on a `dir`
    pool, where Incus has to freeze the container and the sandbox can't, so
    there the drill's snapshot failed, and the failure was reported in the
    log and the alert as it should.
  - `images/brain/check.sh`: 14 of 14; `images/bcast/check.sh`: 8 of 8.
  - **The panic drill:** the screen bcast captures was covered **0.22 s**
    after the request and the target muted in **0.18 s**; both came back
    within 0.2 s of resume. On the encoded stream the cover came 0.79 s
    later still, the encoder's delay on every frame, so what was on screen
    before the panic went out just as late. Input was refused meanwhile.
  - **A panic holds:** through `chat-bcast-scene live` (still muted),
    through brain stopped (bcast kept the cover and the mute) and started
    again (still in panic, input paused); a `brb` scene stayed muted after
    a panic ended.
  - **The dashboard** in Chromium, desktop and phone widths: the audit
    showed every outcome (accepted, invalid, filtered, banned, locked,
    paused), bans with their Unban buttons, the moderation log.
- **Found and fixed while testing:**
  - **The lockdown badge** didn't fit in the corner: now "Regulars only".
  - **The dashboard on phones:** the audit's columns slipped under the
    wrong headings.
  - **The Incus check** passed for the wrong reason (nothing in `infra` had
    the target's address); it now asks for brain's own instance there and
    expects a refusal.

Step 2:

- **1,568 filter tests.** Every list term, in each language, refused:
  - in 11 spellings: case, leetspeak, symbols inside and around, repeats,
    invisible characters, fullwidth, styled letters, accents, in a sentence;
  - spelled out with dots, spaces or hyphens (the `*` terms);
  - as a plural (whole-word terms), and with any separators (phrases);
  - Arabic terms with marks, stretching and the article.
- **Ordinary text still passes:** 31 samples, including "Niger",
  "niggardly", "sniggered", "spice", "raccoon", "Pakistan", "Scunthorpe",
  Spanish "negro" (the color), "en retard", swearing in four languages, and
  IP addresses.
- **Personal details** in 15 forms refused.
- **The rolling buffer:** a term split across three commands is refused;
  backspaces, Enter, and the buffer forgetting old text all handled.
- **Names:** a name with any term is anonymized, including with look-alike
  letters.
- **No crafted input makes a check slow:** about 3 ms at worst.
- **The pipeline and brain:** strikes, timeouts (stored, logged, on the
  overlay, then the user ignored), the filter learning what's typed and
  pressed. 1,880 tests in all.
- **In the sandbox, on the real overlay:** a word typed in two pieces by two
  people was refused at the second, a name holding a listed word showed as
  `viewer#4996`, a troll's three tries showed one refusal and "timed out for
  10 minutes", and a phone number was refused. The word never appeared.
- **Found and fixed while testing:**
  - **A trailing `!` (or `@`, `$`) slipped past whole-word terms:** it was
    read as a letter. Text is now matched twice, symbols as letters and
    symbols as punctuation.
  - **Arabic words starting like a prefix (ل, ال) never matched.**
  - **"spick" (the idiom "spick and span")** came out of the English list,
    and `negro` out of the French one: it's the Spanish word for black.

Step 1:

In the sandbox, both images built and deployed, checked from the Phase 2
desktop:

- **egress, 11 of 12.**
  - **Refused, instantly and without any DNS lookup:** `example.com` over
    HTTP and HTTPS, `1.1.1.1`, brain's feed, the host, CONNECT to port 22,
    port 8080.
  - **Allowed:** HTTPS to the Debian archive and `apt-get update`.
  - **The cap:** an HTTPS download was held to 2.06 MB/s, so it applies to
    tunnels too.
  - **No way around it:** without the proxy, nothing got out.
  - **An allowed name pointing inside:** with a test name allowed but
    resolving to brain's address, the proxy refused it (HTTP and CONNECT).
  - **The one miss, a sandbox limit:** plain HTTP to the archive, because the
    sandbox's own upstream proxy only tunnels HTTPS. On the server Squid goes
    out directly.
- **intranet, 5 of 5.**
  - **Pages:** homepage and Freedoom (28 MB) served.
  - **Wikipedia:** a test ZIM made with Kiwix's own tools, served at
    `/wiki/`.
  - **Reading only:** POST refused, and kiwix-serve unreachable directly.
- **Through chat, end to end.** Console chat through brain opened Firefox on
  the desktop: the intranet homepage, then the offline Wikipedia's library.
- **Bugs found there and fixed:**
  - **Rule order.** The proxy resolved every requested name before refusing
    it. That made refusals slow, and it was a way to send data out through
    DNS (D41). Off-list names are now refused by name alone.
  - **Two build bugs:** an empty Squid include, and a SIGPIPE under
    `pipefail`.
  - **kiwix-serve startup:** it gave up when its ZIM came after it started,
    and it couldn't list the new volume (mode 0711).
