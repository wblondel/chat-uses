#!/usr/bin/env bash
# Check the broadcast pipeline end to end (PLAN.md, Phase 3 "done when").
#
# Inspects the stream inside bcast (a trusted infra container), read over RTMP
# like the forwarder does, and checks: the pipeline is up, the stream's
# format, that the encoder keeps real time, a frame of the stream (saved as
# PNG) with the desktop visible, and that sound played in the target comes
# out of the stream in sync with the picture. Also reports how much CPU bcast
# uses.
#
# The sync test briefly opens a full-screen terminal on the target that
# flashes (reverse video) as a 2 s tone starts: run it before going live.
#
# Usage:
#   sudo images/bcast/check.sh [--frame FILE] [--target NAME]
#
#   --frame FILE   where to save one frame of the stream (default ./stream.png)
#   --target NAME  target instance (project "targets") to play the test tone in;
#                  default: the one at bcast's TARGET_HOST

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

FRAME=stream.png
TARGET=""
while (($#)); do
	case $1 in
	--frame) FRAME=${2:?}; shift ;;
	--target) TARGET=${2:?}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is required"

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
in_bcast() { incus exec bcast --project infra -- "$@"; }
LIVE=rtmp://127.0.0.1:1935/live

incus info bcast --project infra >/dev/null 2>&1 || die "no bcast container: run images/bcast/deploy.sh"
# shellcheck disable=SC2016 # expanded inside bcast
target_host=$(in_bcast bash -c '. /etc/chat-uses/bcast.env; [ -f /etc/chat-uses/bcast.local.env ] && . /etc/chat-uses/bcast.local.env; echo "$TARGET_HOST"')
if [[ -z $TARGET ]]; then
	TARGET=$(incus list --project targets --format csv -c n4 | awk -F, -v ip="$target_host" 'index($2, ip " ") == 1 {print $1; exit}')
fi

log "Pipeline (target $target_host${TARGET:+, instance $TARGET})"
if in_bcast test -d /run/systemd/system 2>/dev/null; then
	for unit in xvfb audio mediamtx websockify audio-in browser encoder forwarder panic; do
		state=$(in_bcast systemctl is-active "chat-bcast-$unit" 2>/dev/null || true)
		if [[ $state == active ]]; then pass "chat-bcast-$unit active"; else fail "chat-bcast-$unit is ${state:-unknown}"; fi
	done
else
	info "systemd is not running in bcast: service checks skipped"
fi
if in_bcast curl -fsS http://127.0.0.1:6080/ 2>/dev/null | grep -q 'chat-uses layout'; then
	pass "layout page served on 127.0.0.1:6080"
else
	fail "layout page not served"
fi
panic=$(in_bcast curl -fsS http://127.0.0.1:6080/control/panic.json 2>/dev/null || true)
case $panic in
*'"panic": false'*) pass "no panic (control/panic.json)" ;;
*'"panic": true'*) fail "a panic is on: the stream is covered and the target muted until a moderator resumes" ;;
*) fail "control/panic.json not served (chat-bcast-panic keeps it)" ;;
esac

log "Stream format"
# r_frame_rate: FLV keeps whole milliseconds (33, 33, 34 ...), so the averaged
# rate reads 1000/33; frames delivered per second are checked below.
probe=$(in_bcast ffprobe -v error -show_entries \
	stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels -of json "$LIVE" 2>/dev/null || true)
summary=$(python3 -c '
import json, sys
try:
    streams = json.loads(sys.argv[1])["streams"]
except Exception:
    print("none"); sys.exit()
v = next((s for s in streams if s["codec_type"] == "video"), {})
a = next((s for s in streams if s["codec_type"] == "audio"), {})
print("%s %sx%s %s + %s %s Hz %s ch" % (v.get("codec_name"), v.get("width"), v.get("height"),
      v.get("r_frame_rate"), a.get("codec_name"), a.get("sample_rate"), a.get("channels")))
' "$probe")
if [[ $summary == "h264 1920x1080 30/1 + aac 48000 Hz 2 ch" ]]; then
	pass "live stream: $summary"
else
	fail "live stream: $summary (expected h264 1920x1080 30/1 + aac 48000 Hz 2 ch)"
fi

log "Real time"
if in_bcast test -d /run/systemd/system 2>/dev/null; then
	progress=$(in_bcast journalctl -u chat-bcast-encoder -n 40 -o cat 2>/dev/null || true)
else
	progress=$(in_bcast tail -n 40 /run/sandbox-units/chat-bcast-encoder.log 2>/dev/null || true)
fi
# The encoder logs a line every 30 s: speed over those 30 s, drops since start.
speed=$(grep -oE 'speed=[0-9.]+x' <<<"$progress" | tail -1 | tr -dc '0-9.' || true)
drops=$(grep -oE 'dropped=[0-9]+' <<<"$progress" | tail -1 | cut -d= -f2 || true)
if [[ -z $speed ]]; then
	info "no encoder report yet (the first comes 30 s after it starts)"
elif awk -v s="$speed" 'BEGIN {exit !(s >= 0.97)}'; then
	pass "encoder speed ${speed}x over the last 30 s, ${drops:-0} frames dropped since it started"
else
	fail "encoder speed ${speed}x over the last 30 s: slower than real time (${drops:-?} frames dropped)"
fi
# CPU time of bcast's running processes, in seconds. The check's own decoder
# has exited by the second sample, so it isn't counted.
cpu_seconds() { in_bcast ps -eo times= | awk '{s += $1} END {print s}'; }
cpu_before=$(cpu_seconds)
t0=$(date +%s.%N)
frames=$(in_bcast timeout -s INT 12 ffmpeg -nostdin -i "$LIVE" -an -f null - 2>&1 |
	grep -oE 'frame= *[0-9]+' | tail -1 | tr -dc '0-9' || true)
t1=$(date +%s.%N)
cpu_after=$(cpu_seconds)
# 12 s of wall time, of which up to 2 s go to connecting and the first keyframe.
if [[ -n $frames ]] && ((frames >= 270)); then
	pass "$frames frames delivered in 12 s (≥ 270 expected at 30 fps)"
else
	fail "${frames:-no} frames delivered in 12 s (≥ 270 expected at 30 fps)"
fi
vcpus=$(awk -v a="$cpu_before" -v b="$cpu_after" -v t0="$t0" -v t1="$t1" 'BEGIN {printf "%.2f", (b - a) / (t1 - t0)}')
info "the pipeline used $vcpus vCPUs over those 12 s"

log "Picture"
in_bcast rm -f /tmp/check-stream.png
# Keyframes only, so the saved frame is complete rather than mid-GOP.
in_bcast ffmpeg -nostdin -loglevel error -skip_frame nokey -i "$LIVE" -frames:v 1 -y /tmp/check-stream.png
incus file pull bcast/tmp/check-stream.png "$FRAME" --project infra
groups=$(in_bcast ffmpeg -nostdin -loglevel error -i /tmp/check-stream.png \
	-vf crop=1600:900:0:0,scale=160:90 -f rawvideo -pix_fmt rgb24 - | python3 -c '
import sys
data = sys.stdin.buffer.read()
print(len({(data[i] >> 3, data[i + 1] >> 3, data[i + 2] >> 3) for i in range(0, len(data), 3)}))')
if ((groups > 50)); then
	pass "desktop visible in the 1600x900 box ($groups colour groups); frame saved to $FRAME"
else
	fail "desktop box looks empty ($groups colour groups); frame saved to $FRAME"
fi

log "Sound and sync"
rms() { # [ffmpeg input options...] -> overall RMS level of the audio, in dB
	in_bcast ffmpeg -nostdin "$@" -vn -af astats -f null - 2>&1 |
		grep -oE 'RMS level dB: [-0-9.inf]+' | tail -1 | awk '{print $4}' || true
}
quiet=$(rms -i "$LIVE" -t 3)
if [[ -z $TARGET ]]; then
	fail "no target instance at $target_host to play a tone in (use --target)"
else
	# In the target: a full-screen terminal that turns to reverse video (dark
	# to light) at the moment a 2 s, 440 Hz tone starts. In a recording of the
	# stream, the offset between the two is the A/V sync.
	tmp=$(mktemp -d)
	python3 -c '
import math, struct, sys
s = [int(12000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000 * 2)]
sys.stdout.buffer.write(b"".join(struct.pack("<hh", v, v) for v in s))' >"$tmp/tone.raw"
	printf '%s\n' '#!/bin/sh' 'sleep 4' "printf '\\033[?5h'" \
		'exec pacat --raw --format=s16le --rate=48000 --channels=2 --latency-msec=20 /tmp/check-tone.raw' >"$tmp/flash.sh"
	incus file push "$tmp/tone.raw" "$TARGET/tmp/check-tone.raw" --project targets --mode 0644
	incus file push "$tmp/flash.sh" "$TARGET/tmp/check-flash.sh" --project targets --mode 0755
	rm -rf "$tmp"
	in_bcast rm -f /tmp/check-sync.mkv
	in_bcast timeout -s INT 12 ffmpeg -nostdin -loglevel error -i "$LIVE" -map 0:v -map 0:a -c copy \
		-y /tmp/check-sync.mkv &
	rec_pid=$!
	sleep 1.5
	incus exec "$TARGET" --project targets --user 1000 --env HOME=/home/chat --env DISPLAY=:0 \
		--env PULSE_SERVER=unix:/run/chat-audio/native -- \
		xfce4-terminal --disable-server --fullscreen --hide-menubar --hide-toolbar --hide-scrollbar \
		-x /tmp/check-flash.sh >/dev/null 2>&1 &
	term_pid=$!
	wait "$rec_pid" || true
	wait "$term_pid" 2>/dev/null || true
	incus exec "$TARGET" --project targets -- rm -f /tmp/check-tone.raw /tmp/check-flash.sh

	# Tone onset: the end of the first silence. Flash: the first frame whose
	# desktop box is mostly light.
	onset=$(in_bcast ffmpeg -nostdin -i /tmp/check-sync.mkv -vn -af silencedetect=n=-35dB:d=0.2 -f null - 2>&1 |
		grep -oE 'silence_end: [0-9.]+' | head -1 | awk '{print $2}' || true)
	flash=$(in_bcast ffmpeg -nostdin -loglevel error -i /tmp/check-sync.mkv -an \
		-vf crop=1600:900:0:0,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=- -f null - 2>/dev/null |
		python3 -c '
import re, sys
t = None
for line in sys.stdin:
    m = re.search(r"pts_time:([0-9.]+)", line)
    if m:
        t = m.group(1)
    m = re.search(r"YAVG=([0-9.]+)", line)
    if m and t is not None and float(m.group(1)) > 150:
        print(t)
        break')
	loud=""
	if [[ -n $onset ]]; then
		loud=$(rms -ss "$(awk -v o="$onset" 'BEGIN {print o + 0.3}')" -i /tmp/check-sync.mkv -t 1.4)
	fi
	if [[ -n $loud && $loud != -inf ]] && awk -v l="$loud" 'BEGIN {exit !(l > -30)}'; then
		pass "a tone played in the target is in the stream (${quiet:-?} dB silent, $loud dB with tone)"
	else
		fail "tone not heard in the stream (${quiet:-?} dB silent, ${loud:-?} dB with tone)"
	fi
	if [[ -n $onset && -n $flash ]]; then
		offset=$(awk -v a="$onset" -v v="$flash" 'BEGIN {printf "%d", (a - v) * 1000}')
		# ITU-R BT.1359: viewers accept sound up to 90 ms early or 185 ms late.
		if ((offset >= -90 && offset <= 185)); then
			pass "sound in sync with the picture: ${offset} ms (positive: sound late)"
		else
			fail "sound ${offset} ms off the picture (positive: late): change AUDIO_DELAY by $((-offset)) ms"
		fi
	else
		fail "sync not measured (tone at ${onset:-?} s, flash at ${flash:-?} s in the recording)"
	fi
fi

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
