#!/usr/bin/env bash
# Check the intranet from a target (PLAN.md 7.2).
#
# From inside a running target: the homepage, the file cabinet with Freedoom,
# the offline Wikipedia (once a ZIM is installed), reading only (a POST is
# refused), and kiwix-serve not reachable directly.
#
# Usage:
#   sudo images/intranet/check.sh [--target NAME]
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
SITE=http://$INTRANET_IP

incus info intranet --project infra >/dev/null 2>&1 || die "no intranet container: run images/intranet/deploy.sh"
if [[ -z $TARGET ]]; then
	TARGET=$(incus list --project targets --format csv -c ns | awk -F, '$2 == "RUNNING" {print $1; exit}')
	[[ -n $TARGET ]] || die "no running target to test from (images/debian-xfce/check.sh --keep starts one)"
fi
in_target() { incus exec -T "$TARGET" --project targets -- "$@"; }
status() { in_target curl -s -o /dev/null --max-time 15 -w '%{http_code}' "$@" 2>/dev/null || true; }

log "Services (intranet $INTRANET_IP, testing from target $TARGET)"
if incus exec intranet --project infra -- test -d /run/systemd/system 2>/dev/null; then
	for unit in nginx chat-kiwix; do
		state=$(incus exec intranet --project infra -- systemctl is-active "$unit" 2>/dev/null || true)
		if [[ $state == active ]]; then pass "$unit active"; else fail "$unit is ${state:-unknown}"; fi
	done
else
	info "systemd is not running in intranet: service checks skipped"
fi

log "Pages and files"
if in_target curl -s --max-time 15 "$SITE/" 2>/dev/null | grep -q "Welcome to the Intranet"; then
	pass "homepage"
else
	fail "no homepage at $SITE/"
fi
size=$(in_target curl -sI --max-time 15 "$SITE/files/freedoom/freedoom2.wad" 2>/dev/null | awk 'tolower($1) == "content-length:" {print $2+0}')
if ((${size:-0} > 10000000)); then
	pass "Freedoom in the file cabinet ($((size / 1000000)) MB)"
else
	fail "no Freedoom WAD at $SITE/files/freedoom/"
fi
code=$(status "$SITE/wiki/")
zims=$(incus exec intranet --project infra -- sh -c 'ls /srv/zim/*.zim 2>/dev/null | xargs -r -n1 basename' | tr '\n' ' ' || true)
if [[ $code == 200 ]]; then
	pass "offline Wikipedia at /wiki/ (${zims:-?})"
	for zim in $zims; do
		[[ $zim == *_nopic_* ]] || fail "$zim has pictures: anything in it can go on stream (deploy.sh --zim with the _nopic_ one)"
	done
elif [[ -z $zims ]]; then
	info "no ZIM installed yet: images/intranet/deploy.sh --zim URL (docs/phase5.md)"
else
	fail "offline Wikipedia: status ${code:-none} (ZIM: $zims)"
fi

log "Reading only"
code=$(status -X POST -d x=1 "$SITE/")
if [[ $code == 405 ]]; then pass "POST refused (405)"; else fail "POST got ${code:-none}"; fi
code=$(status --max-time 5 "http://$INTRANET_IP:8081/")
if [[ $code == 000 ]]; then pass "kiwix-serve not reachable directly (port 8081)"; else fail "port 8081 answered ($code)"; fi

log "Result: $PASS passed, $FAIL failed"
((FAIL == 0))
