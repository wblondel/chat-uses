# Phase 9 — Depth

PLAN.md Phase 9, in this order: macros, the constitution (amendments), the
driver's seat with impeachment, chaos modifiers with the points shop, and
clicking on text. Done when each has simulator coverage and was live-tested
in at least one session.

1. **Macros** (below).
2. The constitution: `!amend`.
3. The driver's seat and `!impeach`.
4. Chaos modifiers and the points shop.
5. `!click "text"`.

## Step 1: macros

PLAN.md 6.10. Chat names a sequence of input commands and votes it in:

```
!macro term = key ctrl+alt+t
!macro hello = type hello world; enter
```

Then anyone types `!term` or `!hello`. `!unmacro NAME` votes one out.

**The grammar.** A name is 2 to 12 letters and digits, starting with a
letter, and can't be a command's (`!click`, `!rewind`, `!amend`…, and the
ones coming: `!buy`, `!chaos`, `!wait`). The commands are separated by `;`,
at most 8, each an input command as chat would type it (`type`, `key`,
`click`, `move`, `drag`, `scroll`, the keys), the `!` optional, with all of
their usual limits. No macro inside a macro, no vote inside one. A refusal
says which command of the macro is wrong, by its number, never repeating
what chat typed.

**The vote.** `!macro` and `!unmacro` are power votes: 60 % yes and a
quorum of 20 % of those active (3 at least), 30 seconds, no veto
countdown, 2 minutes before another (a ruleset can change that, like any
vote's tier, or turn them off). A macro can't take a name in use, and
there are 64 at most: `!unmacro` one first. The text filter checks the
name and every typed text when it's proposed (the vote panel shows it),
and the typed text again, with what was typed just before, each time it
runs: a macro can't launder a slur.

**Running one.** A macro is one command: it rate-limits like one, it's one
line on the overlay (`!hello`), and its commands run one after the other,
0.2 s apart, with nobody else's input in between. In democracy, `!hello` is
a ballot like any command. A macro can't wait for a window to open: keep
`key ctrl+alt+t` and what's typed in it as separate macros.

Macros are kept in brain's store and outlive runs and restarts: chat's
language grows. Each is parsed again when brain starts; one that no longer
parses (a grid that changed) is left out, and the journal says so. A
deletion request takes the proposer's ID off the macros they proposed, not
the macros. The site's Laws page lists them.

Moderators: `chat-brain-admin macros`, `chat-brain-admin macro remove
NAME` (no vote).

**Also in this step.** A refusal on stream can quote chat's word ("unknown
command !word"): any refusal the text filter would refuse is now replaced
on the overlay by fixed wording, so a slur typed as a command never shows.

### What was tested

- `tests/test_macros.py`: the grammar and 16 kinds of bad macro, with the
  reason; no macro in a macro; the filter on the name and text when
  proposed and the text when run, which it learns; the steps' input events
  in order, as one job; brain: voted in, run, kept across a restart,
  refused while it exists, voted out, at most 64, removed by a moderator,
  one that no longer parses left out, a deletion request, the site's list;
  a refusal quoting a slur screened before the overlay.
- `tests/test_parser.py`: macro refusals never repeat what chat wrote.
- The simulator's `macros` scenario (`python -m chatuses.sim --scenarios`):
  20 people vote one in, anyone runs it, it rate-limits as one command,
  four bad ones refused, a slur from the filter's own lists refused before
  any vote, a vote takes it out.

### On the server

```bash
cd /opt/chat-uses && sudo git pull
sudo images/build.sh brain && sudo images/brain/deploy.sh --replace
admin macros                                 # No macros yet: …
```

In a session: `!macro term = key ctrl+alt+t`, 3 `!yes` from people who've
been around 2 minutes, then `!term` opens a terminal.
