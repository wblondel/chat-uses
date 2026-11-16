#!/usr/bin/env bash
# Check brain end to end (PLAN.md, Phase 4).
#
# From the host: the service is up, its health check and overlay feed
# answer (from the host and from bcast, which shows the feed on stream), a
# console chat command reaches the desktop ("!move H5" moves the target's
# pointer to the middle of the screen, and the feed reports who moved it),
# and the chat adapters' state: YouTube reading a video's chat, Discord
# connected. An adapter without its secrets is reported, not failed.
# Moderation (Phase 5): no panic or lockdown on, the admin API refuses
# requests without its token (also from bcast) and answers with it, and
# brain's Incus access (for the panic's forensic snapshot) finds the target.
# It never triggers a panic: images/brain/panic-drill.sh does, on purpose.
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
if [[ $(incus config device get brain data source --project infra 2>/dev/null) == brain-data ]] &&
	in_brain mountpoint -q /var/lib/chat-brain 2>/dev/null; then
	pass "the store is on the brain-data volume (a --replace keeps it)"
else
	fail "the store is on brain's own disk: a --replace would lose it (run images/brain/deploy.sh)"
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
    errors = yt.get("recent_errors") or []
    say("INFO", f"YouTube chat: {state} ({reason or video})" + (f", last error: {errors[-1]}" if errors else ""))
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

log "Screen OCR"
ocr_verdict() {
	python3 -c '
import json, sys
o = json.loads(sys.argv[1] or "{}").get("ocr", {})
if o.get("mode", "off") == "off":
    print("INFO|screen OCR is off (OCR_MODE=off, or no tesseract: see the log)")
elif o.get("reads"):
    print("PASS|screen OCR (%s): reading the screen, %s ms a read, %s hits so far" % (o["mode"], o["last_ms"], o["hits"]))
else:
    print("WAIT|screen OCR (%s) has not read the screen: %s, %s" % (o["mode"], o.get("state"), o.get("last_error") or "no read yet"))
' "$(health)"
}
# The first read comes OCR_INTERVAL seconds after brain connects.
for _ in $(seq 1 15); do
	ocr=$(ocr_verdict)
	[[ $ocr != WAIT* ]] && break
	sleep 1
done
case $ocr in
PASS*) pass "${ocr#*|}" ;;
INFO*) info "${ocr#*|}" ;;
*) fail "${ocr#*|}" ;;
esac

log "Watchdog"
watchdog=$(python3 -c '
import json, sys
w = json.loads(sys.argv[1] or "{}").get("watchdog", {})
if w.get("state") == "healthy":
    print("PASS|watchdog: the target is healthy (" + ", ".join(f"{u} {s}" for u, s in w.get("services", {}).items()) + ")")
else:
    print("FAIL|watchdog: " + ("; ".join(w.get("problems") or []) or w.get("last_error") or "no answer"))
' "$(health)")
case $watchdog in
PASS*) pass "${watchdog#*|}" ;;
*) fail "${watchdog#*|}" ;;
esac

log "Moderation"
api() { # PATH [TOKEN]: the admin API's HTTP status, then its body
	python3 - "http://$BRAIN_IP:$BRAIN_PORT$1" "${2:-}" <<'PY'
import sys, urllib.error, urllib.request as u
req = u.Request(sys.argv[1], headers={"Authorization": f"Bearer {sys.argv[2]}"} if sys.argv[2] else {})
try:
    with u.build_opener(u.ProxyHandler({})).open(req, timeout=5) as resp:
        print(resp.status); print(resp.read().decode())
except urllib.error.HTTPError as err:
    print(err.code)
except OSError as err:
    print(0); print(err)
PY
}
token_file=$CHAT_USES_ETC/secrets/brain-admin-token
if [[ -s $token_file ]]; then
	answer=$(api /api/state "$(cat "$token_file")")
	if [[ $(head -1 <<<"$answer") == 200 ]]; then
		pass "admin API answers with the token (dashboard: http://$BRAIN_IP:$BRAIN_PORT/admin over WireGuard)"
		state=$(tail -n +2 <<<"$answer")
		if [[ $state == *'"panic": null'* ]]; then pass "no panic on"; else fail "a panic is on: the stream is covered until someone resumes"; fi
		if [[ $state == *'"lockdown": null'* ]]; then pass "no lockdown on"; else info "lockdown is on: only regulars can send commands"; fi
	else
		fail "admin API with the token: HTTP $(head -1 <<<"$answer") (was brain deployed after the token was made?)"
	fi
	for bad in "" wrong-token; do
		code=$(api /api/state "$bad" | head -1)
		what="no token"
		[[ -n $bad ]] && what="a wrong token"
		if [[ $code == 401 ]]; then pass "admin API refuses $what"; else fail "admin API with $what: HTTP $code"; fi
	done
	if incus info bcast --project infra >/dev/null 2>&1; then
		# shellcheck disable=SC2016 # expanded inside bcast
		code=$(incus exec bcast --project infra -- curl -s -o /dev/null -w '%{http_code}' "http://$BRAIN_IP:$BRAIN_PORT/api/state" || true)
		if [[ $code == 401 ]]; then pass "from bcast, the admin API refuses too"; else fail "admin API from bcast: HTTP $code"; fi
	fi
else
	fail "no $token_file: run images/brain/deploy.sh (it makes the token)"
fi
# shellcheck disable=SC2016 # expanded inside brain
incus_check=$(in_brain bash -c 'set -a; . /etc/chat-uses/brain.env; [ -f /etc/chat-uses/brain.local.env ] && . /etc/chat-uses/brain.local.env; set +a
	PYTHONPATH=/usr/lib/chat-brain /opt/chat-brain/venv/bin/python -m chatuses.incus' 2>&1 || true)
case $incus_check in
*"; project infra refused") pass "$incus_check (forensic snapshots on panic)" ;;
*"access off"*) fail "$incus_check: run 'bootstrap.sh incus-api', then deploy.sh" ;;
*) fail "${incus_check:-no answer from the Incus check}" ;;
esac

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
