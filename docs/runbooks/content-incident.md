# Content incident

Something on the machine's screen, in the overlay's log, in a name or in
chat that breaks the rules: a slur, personal details, sexual, violent or
shocking content. Also a **content hold**: screen OCR (in enforce mode)
covered the stream by itself with "Content check".

## 1. Panic (seconds)

`/panic what you saw` on Discord, `!panic` in chat, **PANIC** on the
dashboard, or `admin panic`. The stream shows "We'll be right back", the
machine is muted, input is frozen and open votes are cancelled. brain also
takes a **forensic snapshot** of the target (`panic-YYYYmmdd-HHMMSS`):
private, chat can't load it.

During a content hold, a moderator's `/panic` turns it into a panic; either
way, nothing is shown until someone resumes.

## 2. Look (a minute)

- **What was on screen:** what you saw; for a screen OCR hit, the
  screenshot and what it read in `#mod-alerts`; and the dashboard's audit
  log: the last commands, who sent them, what became of each.
- **Who did it:** the audit log, or `admin bans` for who's already out. The
  overlay showed names; `/ban` takes the name it showed, or the ID (`yt:…`,
  `dc:…`).
- **Screen OCR hits:** mark each one in `#mod-alerts` (*Real* / *False
  alarm*), or `admin ocr` then `admin ocr-verdict HIT real|false`. The
  beta's verdicts decide whether enforce mode goes on (`docs/phase7.md`).

## 3. Clean the screen

Input belongs to chat, so a moderator can't just close the window: take
the screen back to before it, the smallest step that removes it.

- **Rewind** to a snapshot from before: `admin snapshots` lists them (the
  number, when, the save names), then `/rewind-to NUMBER` (or a save's
  name) on Discord, or `admin rewind-to NUMBER`. No vote, no life lost;
  `#mod-alerts` says when it's done.
- It's in the files and comes back after a rewind: rewind further, or end
  the run (`admin run end "content"`: a new run from the golden snapshot).

## 4. Deal with who did it

- `/timeout who 60` for a mistake, `/ban who 7d reason` or `/ban who perm
  reason` for intent. Bans work on every platform at once.
- A raid behind it: [raid.md](raid.md).
- A name that is itself the problem: the overlay already shows it as
  `viewer#1234` when the filter catches it; if it didn't, ban, and add the
  word to the lists (step 6).

## 5. Resume

`/resume` (or `admin resume`). The stream comes back as the screen is now.
Say a word in chat if it helps ("back: someone tried something, they're
gone").

## 6. Afterwards

- **The filter missed it?** Add the word or pattern to the word lists
  (`control/src/chatuses/safety/lists/`, `docs/phase5.md` says how), with a
  test, then rebuild and deploy brain (`images/build.sh brain &&
  images/brain/deploy.sh --replace`).
- **The forensic snapshot:** keep it while the incident is open
  (`sudo incus snapshot list chatpc --project targets`); delete it after
  (`sudo incus snapshot delete chatpc panic-… --project targets`): it may
  hold the content itself.
- **A YouTube warning or strike:** read it in YouTube Studio; appeal only if
  it was a false positive. Note it in the post-mortem.
- **The post-mortem:** `docs/incidents/` (no viewer's name or ID).
