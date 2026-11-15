#!/usr/bin/env bash
# Check a chat-uses desktop image (PLAN.md, Phase 2 "done when").
#
# Launches a test target from the image in project "targets" and checks,
# from the host, what bcast and brain will rely on: the desktop over VNC at
# the right size, audio on port 4712, the three services, and how long boot,
# snapshot and restore take.
#
# Usage:
#   sudo images/debian-xfce/check.sh [--image ALIAS] [--instance NAME] [--screenshot FILE] [--keep]
#
#   --image ALIAS     image to test (default chat-uses/debian-xfce)
#   --instance NAME   check an existing target in "targets" instead of launching one
#                     (no boot timing, no snapshot test)
#   --screenshot FILE where to save the desktop screenshot (default ./desktop.png)
#   --keep            leave the test target running
#
# Only point it at images you just built or targets you trust: the probes
# run on the host and read data from the target (they bound every read).

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"
TOOLS=$(cd "$(dirname "${BASH_SOURCE[0]}")/../tools" && pwd)

IMAGE=chat-uses/debian-xfce
INSTANCE=""
SHOT=desktop.png
KEEP=0
while (($#)); do
	case $1 in
	--image) IMAGE=${2:?}; shift ;;
	--instance) INSTANCE=${2:?}; shift ;;
	--screenshot) SHOT=${2:?}; shift ;;
	--keep) KEEP=1 ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is required (bootstrap's packages step installs it)"
load_config

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
now() { date +%s.%N; }
since() { awk -v a="$1" -v b="$(now)" 'BEGIN {printf "%.1f", b - a}'; }
json() { python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else v)' "$1"; }
in_target() { incus exec "$INSTANCE" --project targets -- "$@"; }

launched=0
cleanup() {
	if ((launched && !KEEP)); then
		incus delete --force "$INSTANCE" --project targets >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT

expected_geometry=$(sed -n 's/^GEOMETRY=//p' "$(dirname "${BASH_SOURCE[0]}")/rootfs/etc/chat-uses/target.env")

if [[ -z $INSTANCE ]]; then
	INSTANCE=phase2-check
	ip=${CHATNET_GW%.*}.251
	log "Launching $INSTANCE from $IMAGE at $ip"
	incus delete --force "$INSTANCE" --project targets >/dev/null 2>&1 || true
	t0=$(now)
	# 2 GiB so the test fits next to a real target in the project's memory cap.
	incus launch "$IMAGE" "$INSTANCE" --project targets -p target-desktop \
		--device "eth0,ipv4.address=$ip" -c limits.memory=2GiB >/dev/null
	launched=1
else
	ip=$(incus list "$INSTANCE" --project targets --format csv -c 4 | grep -oE '[0-9]+(\.[0-9]+){3}' | head -1)
	[[ -n $ip ]] || die "$INSTANCE has no IPv4 address"
	t0=""
	log "Checking $INSTANCE at $ip"
fi

log "Boot"
vnc_up=""
for _ in $(seq 1 240); do
	if timeout 2 bash -c "exec 3<>/dev/tcp/$ip/5900" 2>/dev/null; then vnc_up=1; break; fi
	sleep 0.5
done
if [[ -n $vnc_up ]]; then
	pass "VNC answers on $ip:5900${t0:+ ($(since "$t0")s after launch)}"
else
	fail "VNC never answered on $ip:5900"
fi
desktop_up=""
for _ in $(seq 1 120); do
	if in_target pgrep -u chat -x xfdesktop >/dev/null 2>&1; then desktop_up=1; break; fi
	sleep 0.5
done
if [[ -n $desktop_up ]]; then
	pass "Xfce desktop running${t0:+ ($(since "$t0")s after launch)}"
else
	fail "xfdesktop never started"
fi
if in_target test -d /run/systemd/system 2>/dev/null; then
	for unit in chat-xvnc chat-audio chat-audio-pcm chat-session; do
		state=$(in_target systemctl is-active "$unit" 2>/dev/null || true)
		if [[ $state == active ]]; then pass "$unit.service active"; else fail "$unit.service is ${state:-unknown}"; fi
	done
else
	info "systemd is not running in the container: service checks skipped"
fi

log "Display"
sleep 3 # let the panel and wallpaper finish drawing
shot=$(python3 "$TOOLS/vncshot.py" "$ip" 5900 -o "$SHOT" || true)
size="$(json width <<<"$shot")x$(json height <<<"$shot")"
if [[ $size == "$expected_geometry" ]]; then
	pass "framebuffer is $size"
else
	fail "framebuffer is $size, expected $expected_geometry ($(json error <<<"$shot"))"
fi
colours=$(json colours <<<"$shot")
if [[ -n $colours ]] && ((colours > 50)); then
	pass "screen shows a desktop ($colours colour groups, $(json nonblack <<<"$shot") non-black); saved $SHOT"
else
	fail "screen looks empty (${colours:-no} colour groups)"
fi

log "Audio"
silence=$(python3 "$TOOLS/pcmprobe.py" "$ip" 4712 --seconds 1.5 || true)
rate=$(json bytes_per_s <<<"$silence")
if [[ -n $rate ]] && ((rate > 150000 && rate < 230000)); then
	pass "PCM stream on :4712 at $rate B/s (192000 expected)"
else
	fail "PCM stream rate ${rate:-none} ($(json error <<<"$silence"))"
fi
# Play a 440 Hz tone inside the target and check it comes out of :4712.
python3 -c '
import math, struct, sys
frames = [int(12000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000 * 4)]
sys.stdout.buffer.write(b"".join(struct.pack("<hh", s, s) for s in frames))' |
	incus exec "$INSTANCE" --project targets --user 1000 --env HOME=/home/chat \
		--env PULSE_SERVER=unix:/run/chat-audio/native -- \
		pacat --raw --format=s16le --rate=48000 --channels=2 &
tone_pid=$!
sleep 1
tone=$(python3 "$TOOLS/pcmprobe.py" "$ip" 4712 --seconds 1.5 || true)
wait "$tone_pid" 2>/dev/null || true
quiet=$(json rms_dbfs <<<"$silence")
loud=$(json rms_dbfs <<<"$tone")
if [[ -n $loud && -n $quiet ]] && awk -v l="$loud" -v q="$quiet" 'BEGIN {exit !(l > -20 && l > q + 40)}'; then
	pass "a tone played in the desktop is heard on :4712 (${quiet} dBFS silent, ${loud} dBFS with tone)"
else
	fail "tone not heard on :4712 (silent ${quiet:-?} dBFS, tone ${loud:-?} dBFS)"
fi

if ((launched)); then
	log "Snapshots"
	t=$(now)
	if incus snapshot create "$INSTANCE" check --project targets; then
		pass "snapshot created in $(since "$t")s"
		in_target sh -c 'echo changed > /root/check-marker' # on disk: /tmp is tmpfs on Debian 13
		t=$(now)
		if incus snapshot restore "$INSTANCE" check --project targets &&
			! in_target test -e /root/check-marker; then
			pass "snapshot restored in $(since "$t")s while running, change undone"
		else
			fail "snapshot restore failed"
		fi
	else
		fail "snapshot create failed"
	fi
fi

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
