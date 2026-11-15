#!/usr/bin/env bash
# chat-uses server check (PLAN.md, Phase 0).
#
# Reports what the server is (CPU, RAM, disk, virtualization, kernel
# features) and measures CPU steal time under full load, which decides
# whether a 24/7 software-encoded stream will stay smooth.
#
# Usage:
#   sudo infra/host/server-check.sh [--duration SECONDS]   (default 600)
#
# Read-only apart from installing stress-ng if it is missing. Run it before
# Phase 1 and paste the summary into the decision log in PLAN.md.

set -euo pipefail

DURATION=600
while (($#)); do
	case $1 in
	--duration) DURATION=${2:?}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) echo "unknown argument $1" >&2; exit 1 ;;
	esac
	shift
done
if ! [[ $DURATION =~ ^[0-9]+$ ]] || ((DURATION < 30)); then
	echo "--duration must be at least 30 seconds" >&2
	exit 1
fi

say() { printf '%-26s %s\n' "$1" "$2"; }
# feature LABEL YES-TEXT NO-TEXT COMMAND...: report whether COMMAND succeeds.
feature() {
	local label=$1 yes=$2 no=$3
	shift 3
	if "$@" >/dev/null 2>&1; then say "$label" "$yes"; else say "$label" "$no"; fi
}

# Aggregate CPU counters from /proc/stat: "<busy+idle total> <steal>".
cpu_sample() { awk '/^cpu / {t = 0; for (i = 2; i <= 9; i++) t += $i; print t, $9}' /proc/stat; }
steal_pct() { # before after
	awk -v a="$1" -v b="$2" 'BEGIN {split(a, x, " "); split(b, y, " "); dt = y[1] - x[1]; ds = y[2] - x[2]; printf "%.1f", (dt > 0 ? 100 * ds / dt : 0)}'
}

echo "== Server"
# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true
say "OS" "${PRETTY_NAME:-unknown}"
say "Kernel" "$(uname -r)"
say "Virtualization" "$(systemd-detect-virt 2>/dev/null || echo unknown)"
say "CPU model" "$(awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo)"
say "vCPUs" "$(nproc --all)"
say "RAM" "$(awk '/^MemTotal/ {printf "%.1f GiB", $2 / 1048576}' /proc/meminfo)"
say "Swap" "$(awk '/^SwapTotal/ {printf "%.1f GiB", $2 / 1048576}' /proc/meminfo)"
say "Root disk" "$(df -h --output=size,avail / | tail -1 | awk '{print $1 " total, " $2 " free"}')"
say "Default interface" "$(ip -o route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

echo
echo "== Features"
if [[ -e /dev/kvm ]]; then
	say "/dev/kvm" "present (nested virtualization available!)"
else
	say "/dev/kvm" "absent (expected: containers + QEMU software emulation)"
fi
feature "vmx/svm CPU flag" yes no grep -qE '\b(vmx|svm)\b' /proc/cpuinfo
feature "btrfs" available "MISSING (needed for the storage pool)" \
	sh -c 'grep -qw btrfs /proc/filesystems || modprobe -n btrfs'
feature "wireguard" available "MISSING (needed for the admin tunnel)" \
	sh -c '[ -d /sys/module/wireguard ] || modprobe -n wireguard'
feature "binder (Android)" available "missing (only matters for Android, Phase 11)" \
	sh -c '[ -d /sys/module/binder_linux ] || modprobe -n binder_linux'
say "cgroup" "$( [[ -f /sys/fs/cgroup/cgroup.controllers ]] && echo v2 || echo "v1/hybrid (v2 expected on Debian 13)")"

echo
echo "== CPU steal under load (${DURATION}s)"
if ! command -v stress-ng >/dev/null; then
	echo "installing stress-ng..."
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq stress-ng >/dev/null 2>&1
fi

idle_a=$(cpu_sample)
sleep 30
idle_b=$(cpu_sample)
idle_steal=$(steal_pct "$idle_a" "$idle_b")
say "Steal, idle (30s)" "$idle_steal %"

stress-ng --cpu "$(nproc --all)" --timeout "${DURATION}s" --quiet &
stress_pid=$!
trap 'kill "$stress_pid" 2>/dev/null || true' EXIT
sleep 5

load_a=$(cpu_sample)
prev=$load_a
worst=0
elapsed=0
while ((elapsed < DURATION - 10)); do
	sleep 10
	elapsed=$((elapsed + 10))
	now=$(cpu_sample)
	interval=$(steal_pct "$prev" "$now")
	worst=$(awk -v a="$worst" -v b="$interval" 'BEGIN {print (b > a ? b : a)}')
	prev=$now
	printf '\r  %4ds  steal this interval: %5s %%   worst: %5s %%' "$elapsed" "$interval" "$worst"
done
echo
load_steal=$(steal_pct "$load_a" "$prev")
wait "$stress_pid" 2>/dev/null || true
trap - EXIT
say "Steal, full load (avg)" "$load_steal %"
say "Steal, worst 10s" "$worst %"

verdict=$(awk -v s="$load_steal" -v w="$worst" 'BEGIN {
	if (s < 2 && w < 5) print "GOOD: cores behave as dedicated; the stream should stay smooth";
	else if (s < 5 && w < 15) print "OK: some contention; pin the encoder (PLAN.md 5.2) and watch dropped frames";
	else print "POOR: neighbours take significant CPU; expect stutter, consider a Root Server";
}')

echo
echo "== Summary (paste into PLAN.md section 15)"
cat <<EOF
| $(date +%F) | P0 | Server: $(nproc --all) vCPU, $(awk '/^MemTotal/ {printf "%.0f GiB", $2 / 1048576}' /proc/meminfo) RAM, $(systemd-detect-virt 2>/dev/null || echo unknown); steal idle ${idle_steal} %, load avg ${load_steal} %, worst ${worst} % | ${verdict} |
EOF
