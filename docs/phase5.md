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
| 3 | Moderation: panic end to end, cross-platform bans and timeouts, lockdown, moderator slash commands, admin dashboard with audit viewer | |
| 4 | Screen OCR content hold, in shadow mode first | |
| 5 | Watchdog, audio limits, the whole red-team suite (`docs/redteam.md`), stream rules and privacy notice, moderator practice | |

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
- **Lifting a timeout early, or banning by hand:** step 3's moderator
  commands.

## What was tested before this landed

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
