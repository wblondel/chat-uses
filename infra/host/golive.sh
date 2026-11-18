#!/usr/bin/env bash
# The go-live checklist before each session (PLAN.md 10.1): what a script can
# check, then what only a person can.
#
# Usage:
#   sudo infra/host/golive.sh [--drill] [--redteam]
#
#   --drill     practise a panic too (images/brain/panic-drill.sh): the stream
#               shows "We'll be right back" for a few seconds, so do it on the
#               preview or before going live
#   --redteam   run the automated red team too (infra/tests/redteam.sh: long,
#               and it shows a listed word on the target's screen, never
#               printed). Needed after any change to the code or the settings:
#               this script remembers the commit it last passed on and says
#               when that changed
#
# It checks brain, bcast (picture, sound, A/V sync: its test briefly flashes a
# terminal on the target), the target (TARGET_NAME), the host's monitor (#ops
# all good), last night's backup, whether a moderator is around or night mode
# is on, then takes a snapshot of the desktop to rewind to. A few minutes in
# all. Then it lists what's left by hand.
#
# Exit status: 0 if every check passed (a warning doesn't stop a session).

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRILL=0
REDTEAM=0
while (($#)); do
	case $1 in
	--drill) DRILL=1 ;;
	--redteam) REDTEAM=1 ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1 (--drill, --redteam)" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
load_config

VAR=${CHAT_USES_VAR:-/var/lib/chat-uses}
KEEP=$VAR/golive # the frame and screenshot the checks save, and what this script remembers
install -d -m 0755 "$KEEP"
FAILED=()
WARNED=()
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAILED+=("$*"); }
note() { printf '  \033[33m!\033[0m %s\n' "$*"; WARNED+=("$*"); }
admin() { incus exec brain --project infra -- chat-brain-admin "$@"; }
commit=$(git -c safe.directory="$CHAT_USES_REPO" -C "$CHAT_USES_REPO" rev-parse HEAD 2>/dev/null || echo unknown)

log "brain"
if "$CHAT_USES_REPO/images/brain/check.sh"; then
	ok "brain: every check passes"
else
	bad "brain: a check failed (above)"
fi

log "bcast: the stream, its sound and A/V sync"
if "$CHAT_USES_REPO/images/bcast/check.sh" --frame "$KEEP/stream.png"; then
	ok "bcast: every check passes (a frame of the stream: $KEEP/stream.png)"
else
	bad "bcast: a check failed (above)"
fi

log "The target ($TARGET_NAME)"
if "$CHAT_USES_REPO/images/debian-xfce/check.sh" --instance "$TARGET_NAME" --screenshot "$KEEP/desktop.png"; then
	ok "$TARGET_NAME: every check passes (its desktop: $KEEP/desktop.png)"
else
	bad "$TARGET_NAME: a check failed (above)"
fi

log "The host"
if command -v chat-uses-monitor >/dev/null; then
	seen=$(chat-uses-monitor --check 2>&1 || true)
	if grep -qx "monitor: all good" <<<"$seen"; then
		ok "monitor: all good"
	else
		while IFS= read -r line; do
			[[ $line == "monitor: webhook set" ]] || note "${line#monitor: }"
		done < <(grep -v -x "monitor: all good" <<<"$seen")
	fi
	grep -qx "monitor: webhook set" <<<"$seen" && ok "monitor: its alerts reach #ops (webhook set)"
else
	note "the monitor isn't installed: sudo infra/host/bootstrap.sh monitor (docs/phase10.md)"
fi
if [[ -f $VAR/backup.json ]]; then
	ago=$(jq -r 'if (.last_ok | type) == "number" then ((now - .last_ok) / 3600 | floor) else "never" end' "$VAR/backup.json")
	problem=$(jq -r '.error // empty' "$VAR/backup.json")
	if [[ $ago == never ]]; then
		note "no backup has worked yet: journalctl -u chat-uses-backup"
	elif ((ago > 36)); then
		note "the last backup that worked is ${ago} h old: journalctl -u chat-uses-backup"
	else
		ok "backups: the last one worked ${ago} h ago"
	fi
	[[ -z $problem ]] || note "the last backup had a problem: $problem"
else
	note "backups aren't set up: docs/phase10.md, step 3"
fi

log "Moderators"
if night=$(admin night 2>&1); then
	case $night in
	"Night mode: off"*) note "night mode is off: make sure a moderator is online all session ($night)" ;;
	*NIGHT*) ok "no moderator around, and night mode is on: democracy only, bigger votes (a moderator speaking, or /here, brings the day)" ;;
	*) ok "a moderator is around: ${night#Night mode: }" ;;
	esac
else
	bad "brain doesn't say about night mode ($night): an old brain? images/brain/deploy.sh --replace"
fi

if ((REDTEAM)); then
	log "The red team (long)"
	if "$CHAT_USES_REPO/infra/tests/redteam.sh"; then
		[[ $commit == unknown ]] || printf '%s\n' "$commit" >"$KEEP/redteam-passed"
		ok "the red team passes on ${commit:0:7}"
	else
		bad "the red team: a test failed (above, and docs/redteam.md)"
	fi
else
	log "The red team"
	last=$(cat "$KEEP/redteam-passed" 2>/dev/null || true)
	if [[ $commit == unknown ]]; then
		note "can't tell which commit this is (not a git checkout): run the red team after any change"
	elif [[ -n $last && $last == "$commit" ]]; then
		ok "the red team passed on this commit (${commit:0:7})"
	elif [[ -z $last ]]; then
		note "no red team run recorded here: sudo $0 --redteam (long; before going public, and after a change)"
	else
		note "the code changed since the red team last passed (${last:0:7} → ${commit:0:7}): sudo $0 --redteam"
	fi
fi

if ((DRILL)); then
	log "A panic drill (the stream shows \"We'll be right back\" a few seconds)"
	if "$CHAT_USES_REPO/images/brain/panic-drill.sh"; then
		date -u +%FT%TZ >"$KEEP/drill-passed"
		ok "panic drill: covered and muted in time, back after"
	else
		bad "panic drill: failed (above)"
	fi
fi

log "A snapshot to rewind to"
if made=$(admin snapshot 2>&1); then
	ok "$made"
else
	bad "no snapshot: $made"
fi

log "By hand"
cat <<EOF
  ☐ YouTube Studio, the broadcast: ultra-low latency, not made for kids, live
    chat on, slow mode, blocked words
  ☐ 2 minutes on the unlisted preview: sound levels, the overlay, the delay,
    the cursor
EOF
if ((DRILL)); then
	echo "  ✓ a panic dry run (done above)"
else
	echo "  ☐ a panic dry run on the preview: sudo $0 --drill, or /panic then /resume"
fi
cat <<EOF
  ☐ a moderator in #mod-alerts with the dashboard open
    (http://$BRAIN_IP:$BRAIN_PORT/admin over WireGuard)
  ☐ the announcement in Discord's #announcements, with the link
  Then, once live:
    sudo incus exec brain --project infra -- chat-brain-admin youtube https://youtu.be/…
    sudo infra/host/session.py start "NAME"
EOF

echo
if ((${#FAILED[@]})); then
	log "Not ready: ${#FAILED[@]} failed, ${#WARNED[@]} to look at"
	printf '  ✗ %s\n' "${FAILED[@]}"
	exit 1
fi
log "Ready to go live: every check passed${WARNED[*]:+, ${#WARNED[@]} to look at (! above)}"
