#!/usr/bin/env bash
# Network isolation test for chatnet (PLAN.md 7.7, red-team tests 5-7).
#
# Launches throwaway containers, probes what a chat-controlled target can
# reach, and deletes them again. Every "blocked" check aims at a port that is
# really listening, so a pass means the firewall dropped it, not that nothing
# was there. If the real egress and intranet containers (Phase 5) hold the
# addresses the test needs, they're stopped for the test and started again
# after it. Because chatnet has no NAT, a leaked packet to the internet would
# never get a reply, so the internet checks also watch the public interface
# for any packet coming from a target address.
#
# Usage:
#   sudo infra/tests/net-isolation.sh [--keep] [--image IMAGE]
#
#   --image IMAGE   image for the test containers (default images:debian/13)
#   --keep          leave the test containers running for manual poking
#
# Environment:
#   INTERNET_PROBE  host:port on the internet that is normally reachable
#                   (default 1.1.1.1:443). Infra must reach it, targets must not.
#   TEST_TARGET_IP  chatnet address for the test target (default .250)
#   TEST_TARGET_MEMORY  memory limit for the test target, small enough to fit
#                   next to a real target in the project cap (default 512MiB;
#                   empty = use the profile's value)
#
# Exit status: 0 if every check passed.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../host/lib.sh"

IMAGE=images:debian/13
KEEP=0
while (($#)); do
	case $1 in
	--keep) KEEP=1 ;;
	--image) IMAGE=${2:?--image needs a value}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done

[[ $EUID -eq 0 ]] || die "run as root"
command -v tcpdump >/dev/null || die "tcpdump is required: apt-get install tcpdump"
load_config
INTERNET_PROBE=${INTERNET_PROBE:-1.1.1.1:443}
TEST_TARGET_IP=${TEST_TARGET_IP:-${CHATNET_GW%.*}.250}
TEST_TARGET_MEMORY=${TEST_TARGET_MEMORY-512MiB}

TARGET=nettest-target
EGRESS=nettest-egress
INTRANET=nettest-intranet
# Ports opened on the fake infra services in addition to the allowed one.
DECOY_PORTS=(22 443 5900 8080)

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }

PAUSED=() # real infra containers stopped for the test

cleanup() {
	if ((KEEP)); then
		info "kept: $TARGET (targets), $EGRESS, $INTRANET (infra)"
		((${#PAUSED[@]})) && warn "still stopped: ${PAUSED[*]} (start them when done: incus start NAME --project infra)"
		return
	fi
	incus delete --force "$TARGET" --project targets >/dev/null 2>&1 || true
	incus delete --force "$EGRESS" --project infra >/dev/null 2>&1 || true
	incus delete --force "$INTRANET" --project infra >/dev/null 2>&1 || true
	for name in "${PAUSED[@]}"; do
		if incus start "$name" --project infra >/dev/null 2>&1; then info "started $name again"; else warn "could not start $name"; fi
	done
}
trap cleanup EXIT

ip_in_use() {
	incus list --all-projects --format csv -c 4 | grep -qE "(^|[^0-9.])${1//./\\.} \("
}

# The real egress or intranet at its own address: stop it for the test.
pause_real() {
	local name=$1 ip=$2
	[[ $(incus list "$name" --project infra --format csv -c s 2>/dev/null) == RUNNING ]] || return 0
	incus list "$name" --project infra --format csv -c 4 | grep -qF "$ip (" || return 0
	info "stopping $name for the test (it holds $ip; started again afterwards)"
	incus stop "$name" --project infra --timeout 30 >/dev/null 2>&1 || incus stop "$name" --project infra --force
	PAUSED+=("$name")
}

wait_for_ip() {
	local name=$1 project=$2 ip=$3
	for _ in $(seq 1 60); do
		incus list "$name" --project "$project" --format csv -c 4 | grep -qF "$ip (" && return 0
		sleep 1
	done
	die "$project/$name did not get $ip"
}

# Every command in a container runs without a terminal (-T) or stdin (-n).
# From an interactive shell, incus exec would otherwise use a pseudo-terminal,
# and closing it can kill a server setsid has only just forked: on the real
# server every fake service died that way, so the "blocked" checks proved
# nothing (through `| cat` it passed).
cexec() { incus exec -T -n "$@"; }

# can_connect CONTAINER PROJECT HOST PORT: TCP connect from inside a container.
can_connect() {
	cexec "$1" --project "$2" -- timeout 4 bash -c "exec 3<>/dev/tcp/$3/$4" >/dev/null 2>&1
}

expect_open() {
	if can_connect "$1" "$2" "$3" "$4"; then pass "$5"; else fail "$5"; fi
}
expect_blocked() {
	if can_connect "$1" "$2" "$3" "$4"; then fail "$5"; else pass "$5"; fi
}

# start_listener CONTAINER PORT...: tiny TCP servers that answer "ok", then
# wait until each one accepts connections: a check against a port nobody
# listens on would pass for the wrong reason.
start_listener() {
	local name=$1 port
	shift
	# "|| exit 1": dash's `command -v` returns 127 for a missing command, which
	# incus exec reports as "Error: Command not found".
	if cexec "$name" --project infra -- sh -c 'command -v python3 >/dev/null || exit 1'; then
		start_python_listener "$name" "$@"
	else
		cexec "$name" --project infra -- sh -c \
			'command -v socat >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat >/dev/null; }' ||
			die "could not install socat in $name (does infranet reach the internet?)"
		for port in "$@"; do
			cexec "$name" --project infra -- setsid -f socat "TCP-LISTEN:$port,fork,reuseaddr" SYSTEM:'echo ok'
		done
	fi
	for port in "$@"; do
		listening "$name" "$port" || die "$name: nothing listening on port $port, so the checks would prove nothing"
	done
}

# listening CONTAINER PORT: wait up to 10 s for the port to accept locally.
listening() {
	for _ in $(seq 1 20); do
		cexec "$1" --project infra -- timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$2" >/dev/null 2>&1 && return 0
		sleep 0.5
	done
	return 1
}

start_python_listener() {
	local name=$1
	shift
	cexec "$name" --project infra -- setsid -f python3 -c '
import socket, sys, threading
def serve(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen()
    while True:
        c, _ = s.accept(); c.sendall(b"ok\n"); c.close()
for p in sys.argv[1:]:
    threading.Thread(target=serve, args=(int(p),), daemon=True).start()
threading.Event().wait()
' "$@"
}

launch() {
	local name=$1 project=$2 profile=$3 ip=$4
	shift 4
	incus delete --force "$name" --project "$project" >/dev/null 2>&1 || true
	incus launch "$IMAGE" "$name" --project "$project" -p "$profile" \
		--device "eth0,ipv4.address=$ip" "$@" >/dev/null
	wait_for_ip "$name" "$project" "$ip"
}

# --- setup ---------------------------------------------------------------------

log "Preparing test containers (image $IMAGE)"
pause_real egress "$EGRESS_IP"
pause_real intranet "$INTRANET_IP"
for ip in "$EGRESS_IP" "$INTRANET_IP" "$TEST_TARGET_IP"; do
	! ip_in_use "$ip" || die "$ip is already used by an instance; this test needs it free (stop it or change the config)"
done

launch "$EGRESS" infra egress "$EGRESS_IP"
launch "$INTRANET" infra intranet "$INTRANET_IP"
# Small limits so the test fits next to a real target in the project caps.
target_args=(-d "root,size=2GiB")
[[ -n $TEST_TARGET_MEMORY ]] && target_args+=(-c "limits.memory=$TEST_TARGET_MEMORY")
launch "$TARGET" targets target-desktop "$TEST_TARGET_IP" "${target_args[@]}"
start_listener "$EGRESS" "$PROXY_PORT" "$INTRANET_PORT" "${DECOY_PORTS[@]}"
start_listener "$INTRANET" "$INTRANET_PORT" "$PROXY_PORT" "${DECOY_PORTS[@]}"
info "target $TEST_TARGET_IP, egress $EGRESS_IP, intranet $INTRANET_IP"

probe_host=${INTERNET_PROBE%:*}
probe_port=${INTERNET_PROBE##*:}
wan_ip=$(ip -4 -o addr show dev "$WAN_IF" scope global | awk '{split($4, a, "/"); print a[1]; exit}')

# --- checks --------------------------------------------------------------------

log "Allowed paths"
expect_open "$TARGET" targets "$EGRESS_IP" "$PROXY_PORT" "target → egress proxy $EGRESS_IP:$PROXY_PORT"
expect_open "$TARGET" targets "$INTRANET_IP" "$INTRANET_PORT" "target → intranet $INTRANET_IP:$INTRANET_PORT"

log "Internet (test 5)"
capture=$(mktemp)
timeout 25 tcpdump -n -l -i "$WAN_IF" "src net $CHATNET_SUBNET" >"$capture" 2>/dev/null &
capture_pid=$!
sleep 2
if can_connect "$EGRESS" infra "$probe_host" "$probe_port"; then
	pass "control: infra reaches $INTERNET_PROBE"
	expect_blocked "$TARGET" targets "$probe_host" "$probe_port" "target ↛ $INTERNET_PROBE"
else
	fail "control: infra cannot reach $INTERNET_PROBE, so the internet checks prove nothing (set INTERNET_PROBE)"
fi
expect_blocked "$TARGET" targets "$probe_host" 25 "target ↛ $probe_host:25 (SMTP, test 6)"
expect_blocked "$TARGET" targets 8.8.8.8 53 "target ↛ 8.8.8.8:53 (outside DNS)"
# UDP gets no handshake to fail, so only the capture can tell.
cexec "$TARGET" --project targets -- bash -c "echo x >/dev/udp/$probe_host/53; echo x >/dev/udp/8.8.8.8/53" 2>/dev/null || true
sleep 2
kill "$capture_pid" 2>/dev/null || true
wait "$capture_pid" 2>/dev/null || true
# Packet lines start with a timestamp; tcpdump also prints a bare newline on exit.
if grep -qE '^[0-9]' "$capture"; then
	fail "packets from chatnet left on $WAN_IF:"
	grep -E '^[0-9]' "$capture" | head -5 | sed 's/^/         /'
else
	pass "no packet from $CHATNET_SUBNET seen on $WAN_IF (TCP and UDP)"
fi
rm -f "$capture"
# shellcheck disable=SC2016 # awk program, evaluated inside the container
if cexec "$TARGET" --project targets -- awk '$1 !~ /^fe80/ && $1 != "00000000000000000000000000000001" {found = 1} END {exit !found}' /proc/net/if_inet6 2>/dev/null; then
	fail "target has a global IPv6 address"
else
	pass "target has no global IPv6 address"
fi
if cexec "$TARGET" --project targets -- timeout 5 getent hosts deb.debian.org >/dev/null 2>&1; then
	fail "target can resolve names (DNS should be unavailable)"
else
	pass "target cannot resolve names"
fi

log "Host (test 7)"
expect_blocked "$TARGET" targets "$CHATNET_GW" 53 "target ↛ host DNS $CHATNET_GW:53"
expect_blocked "$TARGET" targets "$CHATNET_GW" "$SSH_PORT" "target ↛ host SSH $CHATNET_GW:$SSH_PORT"
expect_blocked "$TARGET" targets "$INFRANET_GW" "$SSH_PORT" "target ↛ host SSH $INFRANET_GW:$SSH_PORT"
[[ -n $wan_ip ]] && expect_blocked "$TARGET" targets "$wan_ip" "$SSH_PORT" "target ↛ host SSH $wan_ip:$SSH_PORT (public address)"
expect_blocked "$TARGET" targets "$CHATNET_GW" "$INCUS_API_PORT" "target ↛ Incus API port $CHATNET_GW:$INCUS_API_PORT"

log "Infrastructure (test 7)"
for port in "${DECOY_PORTS[@]}"; do
	expect_blocked "$TARGET" targets "$EGRESS_IP" "$port" "target ↛ egress $EGRESS_IP:$port"
	expect_blocked "$TARGET" targets "$INTRANET_IP" "$port" "target ↛ intranet $INTRANET_IP:$port"
done
expect_blocked "$TARGET" targets "$INTRANET_IP" "$PROXY_PORT" "target ↛ intranet on the proxy port"
expect_blocked "$TARGET" targets "$EGRESS_IP" "$INTRANET_PORT" "target ↛ egress on the intranet port"

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
