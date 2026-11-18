# Raid

Many new accounts at once: spam, the same command over and over, a vote
pushed by people who just arrived, slurs in names.

New accounts can't vote, push the meter or propose for their first 2
minutes (PLAN.md 6.5), and they raise a vote's quorum rather than swing it.
Rate limits hold the screen. So a raid is mostly noise, but end it fast.

1. **Something bad on screen?** Panic first: [content-incident.md](content-incident.md).
2. **Lockdown:** `/lockdown on` (or `admin lockdown on`). Only regulars,
   members, moderators and people brain has known for a day keep playing;
   everyone else's commands are refused. The overlay shows "Regulars only".
3. **A vote the raid pushed:** `/endvote`.
4. **Bans:** the worst accounts, by the name the overlay showed or by ID
   (`/ban who perm raid`). Three refused slurs in 10 minutes already time
   someone out by themselves.
5. **YouTube chat:** in YouTube Studio, turn on slow mode, or members-only
   chat for a while; add the words they spam to YouTube's blocked words.
6. **Stricter laws for a while:** `/night on` (democracy only, bigger
   quorums, slower commands), `/night auto` afterwards.
7. **When it's over** (10 quiet minutes): `/lockdown off`, `/night auto`.
   Check the screen and rewind if needed (content-incident.md, step 3).
