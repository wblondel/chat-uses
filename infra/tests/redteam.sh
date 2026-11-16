#!/usr/bin/env bash
# The red-team suite (PLAN.md 7.7, docs/redteam.md): try to break the stream
# the way chat would, and check every defence holds.
#
# Runs the automated tests and prints a table to paste into docs/redteam.md:
#   1-4, 21  fork bomb, CPU hog, disk fill, memory hog, escape attempts:
#            on a throwaway target (target-desktop profile, its own address),
#            deleted afterwards
#   5-7      the network isolation test (infra/tests/net-isolation.sh)
#   8, 9     slurs typed whole, in pieces, in leetspeak, with invisible
#            characters and decoded on screen (screen OCR); a slur as a name
#   11       the VNC server stopped: the watchdog brings it back
#   13       full-scale white noise: the stream's loudness stays sane
#   14       50 users flooding brain: rate limits hold, brain's CPU < 50 %
#   16       panic in under a second (images/brain/panic-drill.sh)
#   17, 18   brain or the forwarder killed: the stream goes on
# 10, 12, 15, 19 and 20 are by hand or wait for Phase 6 (docs/redteam.md).
#
# Tests 8, 9, 11, 13, 16 use the real target, brain and bcast: run it on an
# unlisted stream or before going live. It shows a listed word on the
# target's screen (and never prints it).
#
# Usage:
#   sudo infra/tests/redteam.sh [--only 1,8,13] [--image IMAGE] [--report FILE] [--keep]
#
#   --only LIST     run only these tests
#   --image IMAGE   image for the throwaway target (default chat-uses/debian-xfce)
#   --report FILE   also write the results table (Markdown) to FILE
#   --keep          leave the throwaway target running
#
# Environment: TEST_TARGET_IP (default .250 on chatnet), TEST_TARGET_MEMORY
# (default 1GiB), TEST_TARGET_DISK (default 4GiB).
#
# Exit status: 0 if every test that ran passed.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../host/lib.sh"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

ONLY=""
IMAGE=chat-uses/debian-xfce
REPORT=""
KEEP=0
while (($#)); do
	case $1 in
	--only) ONLY=",${2:?},"; shift ;;
	--image) IMAGE=${2:?}; shift ;;
	--report) REPORT=${2:?}; shift ;;
	--keep) KEEP=1 ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is required"
load_config

TEST_TARGET_IP=${TEST_TARGET_IP:-${CHATNET_SUBNET%.*}.250}
TEST_TARGET_MEMORY=${TEST_TARGET_MEMORY:-1GiB}
TEST_TARGET_DISK=${TEST_TARGET_DISK:-4GiB}
RT=redteam-target
LIVE=rtmp://127.0.0.1:1935/live
FEED=ws://$BRAIN_IP:$BRAIN_PORT/ws/overlay
TOOLS=$REPO/images/tools

declare -a ROWS=()
FAILED=0
result() { # NUMBER STATUS TEXT
	ROWS+=("| $1 | $2 | $3 |")
	case $2 in
	PASS) printf '  \033[32mPASS\033[0m %s. %s\n' "$1" "$3" ;;
	FAIL) printf '  \033[31mFAIL\033[0m %s. %s\n' "$1" "$3"; FAILED=$((FAILED + 1)) ;;
	*) printf '  \033[33m%s\033[0m %s. %s\n' "$2" "$1" "$3" ;;
	esac
}
wanted() { [[ -z $ONLY || $ONLY == *",$1,"* ]]; }

in_brain() { incus exec -T brain --project infra -- "$@"; }
in_bcast() { incus exec -T bcast --project infra -- "$@"; }
in_rt() { incus exec -T -n "$RT" --project targets -- "$@"; }
say() { in_brain chat-brain-say "$@" 2>&1 || true; } # its output can hold the word: never print it

health() {
	python3 -c 'import sys, urllib.request as u; print(u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=3).read().decode())' \
		"http://$BRAIN_IP:$BRAIN_PORT/healthz" 2>/dev/null || true
}

# Frames the stream delivers in 12 s (30 fps: 360; check.sh wants 270+).
frames() {
	local out n
	out=$(in_bcast timeout -s INT 12 ffmpeg -nostdin -i "$LIVE" -an -f null - 2>&1 || true)
	n=$(grep -oE 'frame= *[0-9]+' <<<"$out" | tail -1 | grep -oE '[0-9]+' || true)
	echo "${n:-0}"
}

# A limit as the throwaway target really has it (instance or profile).
rt_limit() { # KEY
	incus config show "$RT" --project targets --expanded | awk -v k="  $1:" 'index($0, k) == 1 {gsub(/"/, "", $2); print $2}'
}

pid_of() { # CONTAINER PATTERN
	incus exec -T "$1" --project infra -- pgrep -of "$2" 2>/dev/null || true
}

# The instance holding brain's TARGET_HOST: the real target.
# shellcheck disable=SC2016 # expanded inside brain
target_host=$(in_brain bash -c '. /etc/chat-uses/brain.env; [ -f /etc/chat-uses/brain.local.env ] && . /etc/chat-uses/brain.local.env; echo "$TARGET_HOST"')
TARGET=$(incus list --project targets --format csv -c n4 | awk -F, -v ip="$target_host" 'index($2, ip " ") == 1 {print $1; exit}')
[[ -n $TARGET ]] || die "no target instance holds brain's TARGET_HOST ($target_host)"
in_target() { incus exec -T "$TARGET" --project targets -- "$@"; }

cleanup() {
	if ((!KEEP)) && incus info "$RT" --project targets >/dev/null 2>&1; then
		incus delete --force "$RT" --project targets >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT

log "Red team: target $TARGET ($target_host), throwaway target $TEST_TARGET_IP, $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "?")"

# --- the throwaway target (1-4, 21) ---------------------------------------------

throwaway() {
	if incus info "$RT" --project targets >/dev/null 2>&1; then
		incus delete --force "$RT" --project targets >/dev/null
	fi
	if incus list --all-projects --format csv -c 4 | grep -qF "$TEST_TARGET_IP ("; then
		die "$TEST_TARGET_IP is in use: free it or set TEST_TARGET_IP"
	fi
	info "launching $RT from $IMAGE ($TEST_TARGET_MEMORY, $TEST_TARGET_DISK disk)"
	incus launch "$IMAGE" "$RT" --project targets -p target-desktop \
		--device "eth0,ipv4.address=$TEST_TARGET_IP" -d "root,size=$TEST_TARGET_DISK" \
		-c "limits.memory=$TEST_TARGET_MEMORY" >/dev/null
	for _ in $(seq 1 60); do
		in_rt true 2>/dev/null && return 0
		sleep 1
	done
	die "$RT did not start"
}

restart_rt() {
	incus restart --force "$RT" --project targets >/dev/null 2>&1 || true
	for _ in $(seq 1 60); do
		in_rt true 2>/dev/null && return 0
		sleep 1
	done
}

# The stream keeps up while something hogs the target: frames delivered and
# brain answering, measured while it happens.
stream_holds() { # NUMBER WHAT
	local n=$1 what=$2 delivered status
	delivered=$(frames)
	status=$(health)
	local brain=silent
	[[ $status == *'"ok": true'* ]] && brain=answered
	if ((delivered >= 270)) && [[ $brain == answered ]]; then
		result "$n" PASS "$what: the stream kept $delivered frames in 12 s, brain answered"
	else
		result "$n" FAIL "$what: $delivered frames in 12 s (270+ expected), brain $brain"
	fi
}

t1() {
	local procs limit
	limit=$(rt_limit limits.processes)
	if [[ -z $limit ]]; then # a fork bomb without a cap would take the host down
		result 1 FAIL "the target has no limits.processes: fork bomb not run (bootstrap.sh profiles)"
		return
	fi
	in_rt su chat -c 'setsid sh -c '"'"'f() { f | f & }; f'"'"' >/dev/null 2>&1 &' || true
	sleep 5
	procs=$(incus info "$RT" --project targets | awk '/^ *Processes:/ {print $2}')
	stream_holds 1 "fork bomb ($procs processes, cap $limit)"
	if [[ -n $procs && -n $limit ]] && ((procs <= limit + 50)); then
		result 1 PASS "the fork bomb stayed under limits.processes ($procs of $limit)"
	else
		result 1 FAIL "fork bomb: ${procs:-?} processes, cap ${limit:-?}"
	fi
	restart_rt
}

t2() {
	# shellcheck disable=SC2016 # expanded inside the target
	in_rt sh -c 'for i in $(seq 64); do timeout 40 sh -c "while :; do :; done" & done' >/dev/null 2>&1 &
	sleep 5
	stream_holds 2 "64 busy loops in a target"
	wait || true
	restart_rt
}

t3() {
	local written quota_mib pool_free
	quota_mib=$(numfmt --from=iec-i --to-unit=Mi "${TEST_TARGET_DISK%B}")
	pool_free=$(incus storage info "$POOL_NAME" | awk '/^ *space used:/ {u=$3} /^ *total space:/ {t=$3} END {print t, u}')
	if [[ $(incus storage show "$POOL_NAME" | awk '/^driver:/ {print $2}') == dir ]]; then
		result 3 FAIL "pool $POOL_NAME uses the dir driver, which can't enforce disk sizes: disk fill not run"
		return
	fi
	# Write at most 1.5x the quota, so a missing quota can't fill the host.
	written=$(in_rt sh -c "dd if=/dev/zero of=/fill bs=1M count=$((quota_mib * 3 / 2)) 2>/dev/null; du -m /fill | cut -f1; rm -f /fill" || true)
	if [[ $written =~ ^[0-9]+$ ]] && ((written <= quota_mib)); then
		result 3 PASS "disk fill stopped at the target's quota ($written MiB of $TEST_TARGET_DISK); pool: $pool_free"
	else
		result 3 FAIL "disk fill: ${written:-?} MiB written, quota $TEST_TARGET_DISK"
	fi
}

t4() {
	local code=0 limit
	limit=$(rt_limit limits.memory)
	if [[ -z $limit ]]; then # without a cap the host's OOM killer would pick the victim
		result 4 FAIL "the target has no limits.memory: memory hog not run"
		return
	fi
	in_rt timeout 120 sh -c 'tail /dev/zero' >/dev/null 2>&1 || code=$?
	local state
	state=$(incus list "$RT" --project targets --format csv -c s)
	if [[ $code == 137 && $state == RUNNING ]]; then
		result 4 PASS "memory hog killed by the OOM killer inside the target (limit $limit); the target lives on"
	else
		result 4 FAIL "memory hog: exit $code, target $state"
	fi
	restart_rt
}

t21() {
	local denied=() allowed=()
	check_denied() { # WHAT COMMAND...
		local what=$1
		shift
		if in_rt "$@" >/dev/null 2>&1; then allowed+=("$what"); else denied+=("$what"); fi
	}
	check_denied "modprobe" modprobe dummy
	check_denied "writing /proc/sys" sh -c 'echo 1 > /proc/sys/kernel/sysrq'
	check_denied "a block device node" sh -c 'mknod /tmp/blk b 8 0 && head -c1 /tmp/blk'
	check_denied "mounting a host disk" sh -c 'ls /dev/sd? /dev/vd? /dev/nvme?n? 2>/dev/null | grep -q .'
	check_denied "reading the kernel log" dmesg
	check_denied "raw sockets as the chat user" su chat -c "python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)'"
	if ((${#allowed[@]} == 0)); then
		result 21 PASS "denied: ${denied[*]}"
	else
		result 21 FAIL "allowed: ${allowed[*]} (denied: ${denied[*]})"
	fi
	if in_rt python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)' 2>/dev/null; then
		result 21 INFO "root in the target may open raw sockets in its own network namespace; the firewall still decides where they go (tests 5-7)"
	fi
}

# --- the network (5-7) ---------------------------------------------------------------

t5to7() {
	local out
	if out=$("$HERE/net-isolation.sh" 2>&1); then
		result 5-7 PASS "network isolation: $(grep -oE 'Result: .*' <<<"$out" | tail -1)"
	else
		result 5-7 FAIL "network isolation: $(grep -oE 'Result: .*' <<<"$out" | tail -1) (run infra/tests/net-isolation.sh to see which)"
	fi
}

# --- words (8, 9) ---------------------------------------------------------------------

# A listed English word, whole-word, six letters or more. Never printed.
WORD=$(grep -vE '^[[:space:]]*(#|$|!)' "$REPO/control/src/chatuses/safety/lists/en.txt" | grep -vE '[* ]' | awk 'length($0) >= 6' | head -1)

# Refused before reaching the screen: by the text filter, or by the parser
# (invisible characters can't be typed at all).
refused() { [[ $1 == *"->  filtered"* || $1 == *"->  invalid"* ]]; }

# One command, paced: a burst would meet the rate limits instead of the filter.
try_type() { # NAME TEXT
	sleep 2.5
	say --as "$1" "!type $2"
}

t8() {
	local leet zw third verdicts=() fails=()
	[[ -n $WORD ]] || { result 8 FAIL "no word found in the lists"; return; }
	leet=$(sed 'y/aeios/43105/' <<<"$WORD")
	zw=$(python3 -c 'import sys; print(chr(0x200b).join(sys.argv[1]))' "$WORD")
	third=$(((${#WORD} + 2) / 3))
	if refused "$(try_type rt8whole "$WORD")"; then verdicts+=(whole); else fails+=(whole); fi
	try_type rt8a "${WORD:0:third}" >/dev/null
	try_type rt8b "${WORD:third:third}" >/dev/null
	if refused "$(try_type rt8c "${WORD:2*third}")"; then verdicts+=("in 3 pieces by 3 people"); else fails+=("in pieces"); fi
	sleep 2.5
	say --as rt8d '!key ctrl+u' '!key enter' >/dev/null # forget the pieces
	if refused "$(try_type rt8leet "$leet")"; then verdicts+=(leetspeak); else fails+=(leetspeak); fi
	if refused "$(try_type rt8zw "$zw")"; then verdicts+=("invisible characters"); else fails+=("invisible characters"); fi
	if ((${#fails[@]} == 0)); then
		result 8 PASS "typed slurs refused: ${verdicts[*]}"
	else
		result 8 FAIL "typed slurs got through: ${fails[*]}"
	fi
	t8_ocr
}

ocr_hits() {
	health | python3 -c 'import json, sys; o = json.loads(sys.stdin.read() or "{}").get("ocr", {}); print(o.get("hits", -1) if o.get("mode", "off") != "off" else -1)'
}

t8_ocr() {
	local before after b64 start
	before=$(ocr_hits)
	if ((before < 0)); then
		result 8 SKIP "screen OCR is off: the base64 part can't be checked"
		return
	fi
	b64=$(printf '%s\n' "$WORD" | base64 -w0)
	say --as rt8ocr '!key ctrl+alt+t' >/dev/null
	sleep 4
	start=$(date +%s)
	say --as rt8ocr "!type echo $b64|base64 -d" '!enter' >/dev/null
	for _ in $(seq 1 30); do
		after=$(ocr_hits)
		((after > before)) && break
		sleep 1
	done
	if ((after > before)); then
		result 8 PASS "a slur decoded on screen (echo <base64> | base64 -d) was seen by screen OCR in $(($(date +%s) - start)) s"
	else
		result 8 FAIL "screen OCR didn't see the decoded slur in 30 s"
	fi
	say --as rt8ocr '!type clear' '!enter' '!type exit' '!enter' >/dev/null
}

t9() {
	local feed shown
	feed=$(mktemp)
	python3 "$TOOLS/wsprobe.py" "$FEED" --seconds 6 >"$feed" 2>/dev/null &
	sleep 1
	say --as "${WORD} fan" '!grid' >/dev/null
	wait || true
	shown=$(python3 -c '
import json, sys
for line in open(sys.argv[1]):
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if m.get("type") == "log" and m.get("text") == "!grid":
        print("anonymized" if m["user"].startswith("viewer#") else "SHOWN AS SENT")
' "$feed" | tail -1)
	rm -f "$feed"
	if [[ $shown == anonymized ]]; then
		result 9 PASS "a name holding a slur showed as viewer#NNNN"
	else
		result 9 FAIL "a name holding a slur: ${shown:-not seen on the feed}"
	fi
}

# --- the target's services (11) -------------------------------------------------

t11() {
	if ! in_target systemctl is-system-running >/dev/null 2>&1 && [[ $(in_target systemctl is-system-running 2>&1 || true) != degraded* ]]; then
		result 11 SKIP "the target doesn't run systemd here (the sandbox): stop chat-xvnc by hand on the server"
		return
	fi
	local start back=""
	in_target systemctl stop chat-xvnc
	start=$(date +%s)
	for _ in $(seq 1 150); do
		if in_target systemctl is-active --quiet chat-xvnc && [[ $(health) == *'"target": true'* ]]; then
			back=$(($(date +%s) - start))
			break
		fi
		sleep 1
	done
	if [[ -n $back ]]; then
		result 11 PASS "VNC server stopped: the watchdog started it again, input back in $back s"
	else
		result 11 FAIL "VNC server stopped: not back in 150 s (systemctl start chat-xvnc on $TARGET)"
		in_target systemctl start chat-xvnc || true
	fi
}

# --- sound (13) ------------------------------------------------------------------------

t13() {
	local tmp lufs
	tmp=$(mktemp)
	python3 -c 'import os, sys; sys.stdout.buffer.write(os.urandom(48000 * 4 * 8))' >"$tmp"
	incus file push "$tmp" "$TARGET/tmp/redteam-noise.raw" --project targets --mode 0644
	rm -f "$tmp"
	in_bcast rm -f /tmp/redteam-noise.mkv
	in_bcast timeout -s INT 12 ffmpeg -nostdin -loglevel error -i "$LIVE" -map 0:a -c copy -y /tmp/redteam-noise.mkv &
	sleep 2
	incus exec -T "$TARGET" --project targets --user 1000 --env HOME=/home/chat \
		--env PULSE_SERVER=unix:/run/chat-audio/native -- \
		pacat --raw --format=s16le --rate=48000 --channels=2 /tmp/redteam-noise.raw || true
	wait || true
	in_target rm -f /tmp/redteam-noise.raw
	lufs=$(in_bcast ffmpeg -nostdin -hide_banner -i /tmp/redteam-noise.mkv -af "atrim=3:7,ebur128" -f null - 2>&1 |
		grep -A3 Summary | grep -oE 'I: +-?[0-9.]+' | grep -oE '\-?[0-9.]+' || true)
	if [[ -z $lufs ]] || awk -v l="$lufs" 'BEGIN {exit !(l < -40)}'; then
		result 13 FAIL "the noise didn't reach the stream (${lufs:-no} LUFS): is the target's sound up? (images/bcast/check.sh)"
	elif awk -v l="$lufs" 'BEGIN {exit !(l <= -12)}'; then
		result 13 PASS "full-scale white noise in the target: the stream stayed at $lufs LUFS (YouTube's reference is -14)"
	else
		result 13 FAIL "full-scale white noise: $lufs LUFS on the stream (-12 or below expected)"
	fi
}

# --- flooding brain (14) ---------------------------------------------------------------

t14() {
	local out
	out=$(in_brain env PYTHONPATH=/usr/lib/chat-brain /opt/chat-brain/venv/bin/python - <<'PY'
import asyncio, os, time
from pathlib import Path
from chatuses.console import request

SOCK = Path(os.environ.get("BRAIN_CONSOLE", "/run/chat-brain/console.sock"))
def brain_pid():
    for p in os.listdir("/proc"):
        try:
            if b"chatuses.brain" in Path(f"/proc/{p}/cmdline").read_bytes() and "python" in os.readlink(f"/proc/{p}/exe"):
                return p
        except (OSError, ValueError):
            continue
    raise SystemExit("brain isn't running")
pid = brain_pid()
def cpu():
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
MESSAGES = ["!move A1", "!scroll down", "!zzz", "!type " + "x" * 300, "!" * 200, "!click Q99", "hello"]
outcomes = {}
async def user(n):
    for i in range(80):
        reply = await request(SOCK, {"name": f"flood{n}", "text": MESSAGES[(n + i) % len(MESSAGES)]})
        outcomes[reply["outcome"]] = outcomes.get(reply["outcome"], 0) + 1
        await asyncio.sleep(0.25)
async def main():
    start, c0 = time.monotonic(), cpu()
    await asyncio.gather(*(user(n) for n in range(50)))
    wall = time.monotonic() - start
    print(f"{100 * (cpu() - c0) / wall:.0f} {sum(outcomes.values())} {wall:.0f} " + ",".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
asyncio.run(main())
PY
	) || out="? 0 0 error"
	read -r cpu sent wall outcomes <<<"$out"
	if [[ $cpu =~ ^[0-9]+$ ]] && ((cpu < 50)) && [[ $outcomes == *rate_limited* ]] && [[ $(health) == *'"ok": true'* ]]; then
		result 14 PASS "50 users, $sent messages in $wall s (malformed and too long among them): brain at $cpu % CPU, limits held ($outcomes)"
	else
		result 14 FAIL "flood: brain at ${cpu:-?} % CPU, outcomes: ${outcomes:-none}"
	fi
}

# --- panic (16) ------------------------------------------------------------------------

t16() {
	local out
	if out=$("$REPO/images/brain/panic-drill.sh" 2>&1); then
		result 16 PASS "panic drill: $(grep -oE 'screen covered [0-9.]+ s|target muted [0-9.]+ s' <<<"$out" | tr '\n' ',' | sed 's/,$//; s/,/, /')"
	else
		result 16 FAIL "panic drill: $(grep -E 'FAIL' <<<"$out" | sed 's/\x1b\[[0-9;]*m//g; s/^ *FAIL //' | tr '\n' ';')"
	fi
}

# --- killing pieces (17, 18) -------------------------------------------------------------

t17() {
	local encoder after start back=""
	encoder=$(pid_of bcast "x11grab")
	start=$(date +%s)
	in_brain pkill -9 -f "chatuses.brain" || true
	sleep 1
	for _ in $(seq 1 30); do
		if [[ $(health) == *'"ok": true'* ]]; then
			back=$(($(date +%s) - start))
			break
		fi
		sleep 0.5
	done
	after=$(pid_of bcast "x11grab")
	if [[ -n $back ]] && ((back <= 10)) && [[ -n $encoder && $encoder == "$after" ]]; then
		result 17 PASS "brain killed: back in $back s; the encoder never stopped"
	else
		result 17 FAIL "brain killed: back in ${back:-never} s (10 expected), encoder ${encoder:-?} -> ${after:-?}"
	fi
}

t18() {
	local encoder forwarder after_f after_e
	encoder=$(pid_of bcast "x11grab")
	forwarder=$(pid_of bcast "chat-bcast/forward")
	if [[ -z $forwarder ]]; then
		result 18 SKIP "no forwarder running in bcast"
		return
	fi
	in_bcast pkill -9 -f "chat-bcast/forward" || true
	sleep 10
	after_f=$(pid_of bcast "chat-bcast/forward")
	after_e=$(pid_of bcast "x11grab")
	if [[ -n $after_f && $after_f != "$forwarder" && $after_e == "$encoder" ]]; then
		result 18 PASS "forwarder killed: running again in 10 s; the encoder never stopped (cutting the network is by hand)"
	else
		result 18 FAIL "forwarder killed: forwarder ${after_f:-not running}, encoder $encoder -> ${after_e:-?}"
	fi
}

# --- run -----------------------------------------------------------------------------------

if wanted 1 || wanted 2 || wanted 3 || wanted 4 || wanted 21; then
	log "Throwaway target"
	throwaway
	for n in 1 2 3 4 21; do
		if wanted "$n"; then log "Test $n"; "t$n"; fi
	done
fi
if wanted 5 || wanted 6 || wanted 7; then log "Tests 5-7"; t5to7; fi
for n in 8 9 11 13 14 16 17 18; do
	if wanted "$n"; then log "Test $n"; "t$n"; fi
done
for skipped in "10|sudo rm -rf / in the target: the watchdog sees it today; the death vote and revive come with Phase 6" \
	"12|xrandr: by hand (docs/redteam.md); resetting the resolution comes with Phase 6" \
	"15|new accounts voting: votes come with Phase 6" \
	"19|reboot the host: by hand (docs/redteam.md)" \
	"20|guest kernel panic: QEMU targets come later (Phase 10)"; do
	n=${skipped%%|*}
	if wanted "$n"; then result "$n" SKIP "${skipped#*|}"; fi
done

table=$(printf '| # | Result | Details |\n|---|---|---|\n'; printf '%s\n' "${ROWS[@]}")
log "Results"
printf '%s\n' "$table"
if [[ -n $REPORT ]]; then
	printf '%s\n' "$table" >"$REPORT"
	info "table written to $REPORT"
fi
((FAILED == 0))
