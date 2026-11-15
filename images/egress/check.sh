#!/usr/bin/env bash
# Check the egress proxy from a target (PLAN.md 7.2, red-team tests 5-7).
#
# From inside a running target (where chat is), through the proxy: the
# allowlisted Debian archive works over HTTP and HTTPS and apt can update;
# other sites, addresses typed directly, private addresses (brain's feed on
# infranet), CONNECT to other ports and other ports all refused; downloads
# are capped near EGRESS_BYTES_PER_SECOND. Without the proxy, no way out.
#
# Usage:
#   sudo images/egress/check.sh [--target NAME]
#
#   --target NAME  target instance (project "targets") to test from;
#                  default: the first running one

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

TARGET=""
while (($#)); do
	case $1 in
	--target) TARGET=${2:?}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
load_config

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
PROXY=http://$EGRESS_IP:$PROXY_PORT

incus info egress --project infra >/dev/null 2>&1 || die "no egress container: run images/egress/deploy.sh"
if [[ -z $TARGET ]]; then
	TARGET=$(incus list --project targets --format csv -c ns | awk -F, '$2 == "RUNNING" {print $1; exit}')
	[[ -n $TARGET ]] || die "no running target to test from (images/debian-xfce/check.sh --keep starts one)"
fi
in_target() { incus exec -T "$TARGET" --project targets -- "$@"; }
# curl in the target; prints "<CONNECT reply> <HTTP status> <bytes/s>".
fetch() { in_target curl -s -o /dev/null --max-time "${TIME:-15}" -w '%{http_connect} %{http_code} %{speed_download}' "$@" 2>/dev/null || true; }

log "Proxy (egress $EGRESS_IP, testing from target $TARGET)"
if incus exec egress --project infra -- test -d /run/systemd/system 2>/dev/null; then
	state=$(incus exec egress --project infra -- systemctl is-active squid 2>/dev/null || true)
	if [[ $state == active ]]; then pass "squid active"; else fail "squid is ${state:-unknown}"; fi
else
	info "systemd is not running in egress: service check skipped"
fi

log "Allowed: the Debian archive"
read -r _ code _ <<<"$(fetch -x "$PROXY" -I http://deb.debian.org/debian/dists/trixie/InRelease)"
if [[ $code == 200 ]]; then pass "HTTP to deb.debian.org"; else fail "HTTP to deb.debian.org: status ${code:-none}"; fi
read -r connect _ _ <<<"$(fetch -x "$PROXY" -I https://deb.debian.org/debian/dists/trixie/InRelease)"
if [[ $connect == 200 ]]; then pass "HTTPS (CONNECT) to deb.debian.org:443"; else fail "CONNECT to deb.debian.org:443: ${connect:-none}"; fi
if in_target timeout 120 apt-get update -qq >/dev/null 2>&1; then
	pass "apt-get update in the target (its apt points at the proxy)"
else
	fail "apt-get update failed in the target"
fi

log "Refused (tests 5-7)"
refused() { # description, then curl arguments
	local what=$1 out connect code
	shift
	out=$(fetch -x "$PROXY" "$@")
	read -r connect code _ <<<"$out"
	if [[ $connect == 403 || ($connect == 000 && $code == 403) ]]; then
		pass "$what"
	else
		fail "$what: got CONNECT ${connect:-?}, HTTP ${code:-?}"
	fi
}
refused "a site not on the list (http://example.com)" http://example.com/
refused "the same over HTTPS" https://example.com/
refused "an address typed directly (http://1.1.1.1)" http://1.1.1.1/
refused "brain's feed on infranet (private address)" "http://$BRAIN_IP:$BRAIN_PORT/healthz"
refused "the host's address on infranet" "http://$INFRANET_GW/"
refused "CONNECT to an allowed name on another port (22)" -p http://deb.debian.org:22/
refused "plain HTTP on another port (8080)" http://deb.debian.org:8080/

log "Bandwidth"
cap=$EGRESS_BYTES_PER_SECOND
read -r _ code speed <<<"$(TIME=60 fetch -x "$PROXY" http://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz)"
speed=${speed%.*}
if [[ $code != 200 || -z $speed ]]; then
	fail "test download failed (status ${code:-none})"
elif ((speed <= cap * 12 / 10)); then
	pass "a download ran at $((speed / 1000)) kB/s, within the cap of $((cap / 1000)) kB/s"
else
	fail "a download ran at $((speed / 1000)) kB/s, over the cap of $((cap / 1000)) kB/s"
fi

log "No way around it"
read -r _ code _ <<<"$(TIME=8 fetch --noproxy '*' -I http://deb.debian.org/)"
if [[ $code == 000 ]]; then pass "without the proxy: no connection"; else fail "without the proxy: status $code"; fi

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
