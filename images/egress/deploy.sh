#!/usr/bin/env bash
# Deploy the egress proxy container, egress (PLAN.md 7.2, Phase 5).
#
# Usage:
#   sudo images/egress/deploy.sh             create egress if missing, else restart Squid
#   sudo images/egress/deploy.sh --replace   recreate egress from the latest chat-uses/egress image
#                                            (targets lose the internet for a few seconds)
#   --image ALIAS                            launch from another image (default chat-uses/egress)
#
# The allowlist is part of the image (images/egress/rootfs/etc/squid/
# chat-uses-allow.txt): change it there, rebuild, deploy with --replace.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

IMAGE=chat-uses/egress
NAME=egress
REPLACE=0
while (($#)); do
	case $1 in
	--replace) REPLACE=1 ;;
	--image) IMAGE=${2:?}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
load_config

in_egress() { incus exec "$NAME" --project infra -- "$@"; }

exists=0
incus info "$NAME" --project infra >/dev/null 2>&1 && exists=1
if ((exists && REPLACE)); then
	log "Removing the old $NAME"
	incus delete --force "$NAME" --project infra >/dev/null
	exists=0
fi
if ((!exists)); then
	log "Launching $NAME from $IMAGE at $EGRESS_IP"
	incus launch "$IMAGE" "$NAME" --project infra -p egress --device "eth0,ipv4.address=$EGRESS_IP" >/dev/null
	for _ in $(seq 1 60); do
		in_egress true >/dev/null 2>&1 && break
		sleep 1
	done
fi

log "Restarting Squid"
if in_egress test -d /run/systemd/system; then
	in_egress systemctl restart squid.service
	sleep 2
	in_egress systemctl --no-pager --plain list-units squid.service | sed -n '1,2p' | sed 's/^/    /'
else
	warn "systemd is not running in $NAME; Squid was not restarted"
fi
info "check: images/egress/check.sh"
