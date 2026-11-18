# Stream down

YouTube shows the stream offline, frozen, or without sound. `#ops` usually
says so first: brain's "📡 The stream isn't reaching YouTube" (bcast's
forwarder failing), or the host's monitor ("the encoder hasn't reported",
"the bcast container isn't running").

The chain: the target's desktop and sound → **bcast** (the layout page in
Chromium, the encoder, MediaMTX, the forwarder) → YouTube. Find where it
breaks, from the end.

```sh
admin() { sudo incus exec brain --project infra -- chat-brain-admin "$@"; }
admin status | grep -E '^(Stream|Machine):'
sudo chat-uses-monitor --check
```

## 1. The forwarder (bcast → YouTube)

`Stream: NOT reaching YouTube` means the forwarder can't hold its
connection. Read its log **masked** (the raw log holds the stream key):

```sh
sudo incus exec bcast --project infra -- bash -c 'k=$(tr -d "[:space:]" </etc/chat-uses/secrets/youtube-stream-key 2>/dev/null); journalctl -u chat-bcast-forwarder -b --no-pager -n 15 -o short-iso | sed -E "s#live2/[^ ]+#live2/<key>#g" | { if [ -n "$k" ]; then sed "s#$k#<key>#g"; else cat; fi; }'
```

- Failing at once every 5 s ("Error in the pull function", "Input/output
  error"): YouTube hangs up, most often because it no longer takes the
  stream key. In YouTube Studio, check the broadcast exists and isn't
  ended; take its stream key (or a new one), put it in
  `/etc/chat-uses/secrets/youtube-stream-key` (`sudo sh -c 'umask 077;
  cat >/etc/chat-uses/secrets/youtube-stream-key'`, paste, Enter, Ctrl-D)
  and `sudo images/bcast/deploy.sh`.
- Timeouts, DNS errors: the server's network (step 4).

## 2. The encoder and MediaMTX (inside bcast)

```sh
sudo incus exec bcast --project infra -- systemctl --no-pager status 'chat-bcast-*'   # which are active
sudo incus exec bcast --project infra -- journalctl -u chat-bcast-encoder -n 20 -o cat --no-pager
sudo incus exec bcast --project infra -- journalctl -u chat-bcast-mediamtx -n 20 -o cat --no-pager
```

(Never `journalctl -u 'chat-bcast-*'` to paste somewhere: that includes the
forwarder's log.) The encoder logs `speed=…x` every 30 s: below 0.97 it
can't keep up (the CPU: `docs/phase3.md`, `X264_PRESET`). Restart the whole
pipeline:

```sh
sudo incus exec bcast --project infra -- systemctl restart chat-bcast.target
sudo images/bcast/check.sh        # picture, sound, A/V sync
```

Still broken after a restart and a redeploy: roll bcast back to its last
good build (`sudo incus image alias list --project infra | grep chat-uses/bcast/`,
then `sudo images/bcast/deploy.sh --replace --image chat-uses/bcast/YYYYmmdd-HHMM`).

## 3. No picture of the desktop, or no sound

The stream is up but shows "Reconnecting to the machine…" or is silent: the
target. `sudo images/debian-xfce/check.sh --instance chatpc`; brain's
watchdog restarts the target's services by itself and tells `#ops`. A
target that's off: `sudo incus start chatpc --project targets`.

## 4. The server's network

```sh
timeout 5 bash -c '</dev/tcp/a.rtmps.youtube.com/443' && echo "YouTube's ingest: reachable"
timeout 5 bash -c '</dev/tcp/1.1.1.1/443' && echo "the internet: reachable"
```

Nothing answers: netcup's side (their status page, from the customer
control panel). YouTube's side: YouTube Studio's stream health panel, and
TeamYouTube's posts.

## 5. While it's down

- Post in Discord's `#announcements` (a minute is enough: "stream's down,
  on it").
- To show something rather than nothing once the pipeline is back:
  `sudo incus exec bcast --project infra -- chat-bcast-scene brb "Back in five" "what happened"`,
  then `chat-bcast-scene live`.
- A long outage ends the YouTube broadcast: a new one means a new link on
  the site (`site/pages/about.md`) and `admin youtube NEW_VIDEO`.
