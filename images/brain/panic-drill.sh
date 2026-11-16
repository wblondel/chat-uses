#!/usr/bin/env bash
# Practise a panic and time it (PLAN.md 7.5; red-team test 16: under 1 s).
#
# Triggers a real panic through brain, as /panic on Discord or the dashboard's
# button would, and measures on the stream itself, inside bcast:
#   - cover: from the panic to the first stream frame (what goes to YouTube)
#     where the desktop is covered;
#   - sound: until the target's sound is muted in the mix;
# and checks that input is refused while it lasts, that #mod-alerts got the
# alert (or that it waits for Discord), and the forensic snapshot. Then it
# resumes and checks the stream is back, picture and sound.
#
# The stream shows "We'll be right back" for a few seconds: run it on an
# unlisted stream or before going live. The drill's snapshot is deleted
# afterwards unless --keep-snapshot.
#
# Usage:
#   sudo images/brain/panic-drill.sh [--keep-snapshot]

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

KEEP=0
case ${1:-} in
"") ;;
--keep-snapshot) KEEP=1 ;;
-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
*) die "unknown argument $1" ;;
esac
[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is required"
load_config

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
in_brain() { incus exec brain --project infra -- "$@"; }
in_bcast() { incus exec bcast --project infra -- "$@"; }
LIMIT=1.0 # seconds

incus info brain --project infra >/dev/null 2>&1 || die "no brain container"
incus info bcast --project infra >/dev/null 2>&1 || die "no bcast container"
if in_brain chat-brain-admin status | grep -q '^PANIC'; then
	die "a panic is already on: resume it first (chat-brain-admin resume)"
fi

# In bcast: decode the stream as tiny grey pictures of the desktop's area
# (1600x900 at the top left), each stamped with the time it came out; and
# watch the target's mute. Both run until the drill is over.
WATCH=$(
	cat <<'PY'
import json, os, subprocess, sys, threading, time
seconds = float(sys.argv[1])
end = time.time() + seconds
frames, mutes = [], []

def watch_mute():
    env = {**os.environ, "PULSE_SERVER": "unix:/run/chat-bcast-audio/native"}
    while time.time() < end:
        out = subprocess.run(["pactl", "get-sink-mute", "target"], capture_output=True, text=True, env=env).stdout
        mutes.append((time.time(), "yes" in out))
        time.sleep(0.02)

threading.Thread(target=watch_mute, daemon=True).start()
ff = subprocess.Popen(
    ["ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
     "-i", "rtmp://127.0.0.1:1935/live", "-an", "-vf", "crop=1600:900:0:0,scale=32:18",
     "-pix_fmt", "gray", "-f", "rawvideo", "-"],
    stdout=subprocess.PIPE)
while time.time() < end:
    frame = ff.stdout.read(32 * 18)
    if len(frame) < 32 * 18:
        break
    frames.append((time.time(), frame.hex()))
ff.kill()
print(json.dumps({"frames": frames, "mutes": mutes}))
PY
)
record=$(mktemp)
trap 'rm -f "$record"' EXIT

log "Panic drill"
in_bcast python3 -c "$WATCH" 12 >"$record" &
watcher=$!
sleep 4 # the decoder is up and a few normal frames are in
t_panic=$(date +%s.%N)
reply=$(in_brain chat-brain-admin panic "drill (images/brain/panic-drill.sh)" 2>&1 || true)
if [[ $reply == "Panic on:"* ]]; then pass "panic on: $reply"; else fail "panic: $reply"; fi
sleep 1
paused=$(in_brain chat-brain-say --as drill '!key enter' 2>&1 || true)
if [[ $paused == *"->  paused"* ]]; then pass "input refused during the panic"; else fail "input during the panic: $paused"; fi
sleep 3
t_resume=$(date +%s.%N)
reply=$(in_brain chat-brain-admin resume 2>&1 || true)
if [[ $reply == "Resumed:"* ]]; then pass "resumed"; else fail "resume: $reply"; fi
wait "$watcher" || true

verdicts=$(python3 - "$record" "$t_panic" "$t_resume" "$LIMIT" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
t_panic, t_resume, limit = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
frames = [(t, bytes.fromhex(h)) for t, h in data["frames"]]
def say(kind, text): print(f"{kind}|{text}")
before = [f for t, f in frames if t < t_panic]
during = [(t, f) for t, f in frames if t_panic <= t < t_resume]
if len(before) < 5 or len(during) < 10:
    say("FAIL", f"not enough of the stream recorded ({len(before)} frames before, {len(during)} during)")
    sys.exit()
def diff(a, b): return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
live, covered = before[-1], during[-1][1]  # the desktop just before; the cover once settled
if diff(live, covered) < 8:
    say("FAIL", "the stream didn't change: no cover on the stream")
else:
    first = next((t for t, f in during if diff(f, covered) < diff(f, live)), None)
    delay = first - t_panic
    say("PASS" if delay < limit else "FAIL", f"stream covered {delay:.2f} s after the panic (limit {limit:.0f} s)")
    after = [(t, f) for t, f in frames if t >= t_resume]
    back = next((t for t, f in after if diff(f, live) < diff(f, covered)), None)
    if back is None:
        say("FAIL", "the stream still looked covered after resume")
    else:
        say("PASS", f"stream uncovered {back - t_resume:.2f} s after resume")
mutes = data["mutes"]
was_muted = any(m for t, m in mutes if t < t_panic)
muted = next((t for t, m in mutes if t >= t_panic and m), None)
if was_muted:
    say("INFO", "the target was already muted before the drill (a scene?): mute not timed")
elif muted is None:
    say("FAIL", "the target's sound was never muted (is chat-bcast-panic running in bcast?)")
else:
    delay = muted - t_panic
    say("PASS" if delay < limit else "FAIL", f"target muted {delay:.2f} s after the panic")
    unmuted = next((t for t, m in mutes if t >= t_resume and not m), None)
    if unmuted is None:
        say("FAIL", "the target's sound didn't come back after resume")
    else:
        say("PASS", f"sound back {unmuted - t_resume:.2f} s after resume")
PY
)
while IFS='|' read -r kind text; do
	case $kind in
	PASS) pass "$text" ;;
	FAIL) fail "$text" ;;
	*) info "$text" ;;
	esac
done <<<"$verdicts"

log "Alert and forensic snapshot"
token_file=$CHAT_USES_ETC/secrets/brain-admin-token
events=$(python3 - "http://$BRAIN_IP:$BRAIN_PORT/api/events?limit=20" "$token_file" <<'PY'
import json, sys, urllib.request as u
token = open(sys.argv[2]).read().strip()
req = u.Request(sys.argv[1], headers={"Authorization": f"Bearer {token}"})
print(json.dumps(json.load(u.build_opener(u.ProxyHandler({})).open(req, timeout=5))["events"]))
PY
) || events="[]"
snapshot=$(python3 -c '
import json, sys
events = json.loads(sys.argv[1])
snap = next((e for e in events if e["type"] == "snapshot"), None)
print(" ".join([snap.get("result", "?"), snap.get("instance", ""), snap.get("name", ""), snap.get("error", "")]) if snap else "none")
' "$events")
read -r result instance name error <<<"$snapshot"
case $result in
ok) pass "forensic snapshot $instance/$name taken" ;;
off) fail "no forensic snapshot: brain has no Incus access (bootstrap.sh incus-api, then images/brain/deploy.sh)" ;;
failed) fail "forensic snapshot failed: $instance $name $error" ;;
*) fail "no snapshot event after the panic" ;;
esac
if [[ $result == ok && $KEEP == 0 ]]; then
	incus snapshot delete "$instance" "$name" --project targets && info "drill snapshot deleted"
fi
alerts=$(in_brain chat-brain-admin status 2>&1 | sed -n 's/.*Discord: \([a-z]*\).*/\1/p')
if [[ $alerts == connected ]]; then
	info "Discord connected: check #mod-alerts for the panic, resume and snapshot posts"
else
	info "Discord ${alerts:-off}: the alerts wait in brain until it connects (or go nowhere without a token)"
fi

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
