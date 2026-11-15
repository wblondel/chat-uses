#!/usr/bin/env bash
# Check brain end to end (PLAN.md, Phase 4).
#
# From the host: the service is up, its health check and overlay feed
# answer (from the host and from bcast, which shows the feed on stream), a
# console chat command reaches the desktop ("!move H5" moves the target's
# pointer to the middle of the screen, and the feed reports who moved it),
# and the chat adapters' state: YouTube reading a video's chat, Discord
# connected. An adapter without its secrets is reported, not failed.
#
# Usage:
#   sudo images/brain/check.sh

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

case ${1:-} in
"") ;;
-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
*) die "unknown argument $1" ;;
esac
[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is required"
load_config

TOOLS="$(dirname "${BASH_SOURCE[0]}")/../tools"
PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
in_brain() { incus exec brain --project infra -- "$@"; }
FEED=ws://$BRAIN_IP:$BRAIN_PORT/ws/overlay

incus info brain --project infra >/dev/null 2>&1 || die "no brain container: run images/brain/deploy.sh"
# shellcheck disable=SC2016 # expanded inside brain
target_host=$(in_brain bash -c '. /etc/chat-uses/brain.env; [ -f /etc/chat-uses/brain.local.env ] && . /etc/chat-uses/brain.local.env; echo "$TARGET_HOST"')

log "Service (target $target_host)"
if in_brain test -d /run/systemd/system 2>/dev/null; then
	state=$(in_brain systemctl is-active chat-brain 2>/dev/null || true)
	if [[ $state == active ]]; then pass "chat-brain active"; else fail "chat-brain is ${state:-unknown}"; fi
else
	info "systemd is not running in brain: service check skipped"
fi
health() {
	# Straight to brain, whatever http_proxy says.
	python3 -c 'import sys, urllib.request as u; print(u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=3).read().decode())' \
		"http://$BRAIN_IP:$BRAIN_PORT/healthz" 2>/dev/null || true
}
# Up to 20 s: brain may still be starting, and connects to VNC after it.
for _ in $(seq 1 20); do
	status=$(health)
	[[ $status == *'"target": true'* ]] && break
	sleep 1
done
if [[ $status == *'"ok": true'* ]]; then pass "health check answers on $BRAIN_IP:$BRAIN_PORT"; else fail "no health check on $BRAIN_IP:$BRAIN_PORT"; fi
if [[ $status == *'"target": true'* ]]; then
	pass "connected to the desktop's VNC at $target_host"
else
	fail "not connected to VNC at $target_host (is the target running? TARGET_HOST in brain.local.env?)"
fi

log "Overlay feed"
if python3 "$TOOLS/wsprobe.py" "$FEED" --seconds 3 --until-type hello >/dev/null; then
	pass "feed answers from the host"
else
	fail "no feed at $FEED"
fi
if incus info bcast --project infra >/dev/null 2>&1; then
	if incus exec bcast --project infra -- python3 - "$FEED" --seconds 3 --until-type hello <"$TOOLS/wsprobe.py" >/dev/null; then
		pass "feed answers from bcast (the layout page's side)"
	else
		fail "bcast can't reach the feed at $FEED"
	fi
	# shellcheck disable=SC2016 # expanded inside bcast
	url=$(incus exec bcast --project infra -- bash -c '. /etc/chat-uses/bcast.env; [ -f /etc/chat-uses/bcast.local.env ] && . /etc/chat-uses/bcast.local.env; echo "$BRAIN_URL"')
	if [[ $url == "$FEED" ]]; then pass "bcast's layout page uses this feed"; else fail "bcast's BRAIN_URL is $url, not $FEED"; fi
else
	info "no bcast container: its side skipped"
fi

log "Console chat to the desktop"
events=$(mktemp)
trap 'rm -f "$events"' EXIT
python3 "$TOOLS/wsprobe.py" "$FEED" --seconds 8 --until-type pointer >"$events" 2>&1 &
probe=$!
sleep 1
reply=$(in_brain chat-brain-say --as check '!move H5' 2>&1 || true)
if [[ $reply == *" ->  accepted"* ]]; then pass "chat-brain-say: !move H5 accepted"; else fail "chat-brain-say: ${reply:-no reply}"; fi
wait "$probe" || true
if grep -q '"type": "pointer", "x": 750, "y": 450, "user": "check"' "$events"; then
	pass "the pointer moved to H5 and the feed showed who moved it"
else
	fail "no pointer event on the feed ($(grep -o '"state": "[a-z]*"' "$events" | tail -1))"
fi

log "Chat adapters"
# Discord takes a few seconds to log in after brain starts.
for _ in $(seq 1 20); do
	status=$(health)
	[[ $status != *'"state": "connecting"'* ]] && break
	sleep 1
done
verdicts=$(python3 -c '
import json, sys
chat = json.loads(sys.argv[1] or "{}").get("chat", {})
yt, dc = chat.get("youtube", {}), chat.get("discord", {})
def say(kind, text): print(f"{kind}|{text}")
state, reason, video = yt.get("state"), yt.get("reason") or "", yt.get("video")
if state == "streaming":
    say("PASS", f"YouTube chat: reading the chat of {video}")
elif state in ("looking", "waiting", "reconnecting"):
    say("INFO", f"YouTube chat: {state} ({reason or video})")
elif state == "off" and reason:
    say("INFO", "YouTube chat off: no API key (/etc/chat-uses/secrets/google-api-key on the host, then deploy.sh)")
elif state == "off":
    say("INFO", "YouTube chat: no video set (chat-brain-admin youtube VIDEO_ID_OR_URL)")
elif state == "ended":
    say("INFO", f"YouTube chat: {reason}")
else:
    say("FAIL", f"YouTube chat {state}: {reason}")
state, reason, channel = dc.get("state"), dc.get("reason") or "no reason given", dc.get("channel")
if state == "connected" and not dc.get("reason"):
    say("PASS", f"Discord: connected, reading #{channel}")
elif state == "off":
    say("INFO", "Discord off: needs the bot token and DISCORD_GUILD_ID (docs/phase4.md, step 4)")
else:
    say("FAIL", f"Discord {state}: {reason}")
' "$status")
while IFS='|' read -r kind text; do
	case $kind in
	PASS) pass "$text" ;;
	FAIL) fail "$text" ;;
	*) info "$text" ;;
	esac
done <<<"$verdicts"

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
