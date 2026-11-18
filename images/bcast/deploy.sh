#!/usr/bin/env bash
# Deploy the broadcaster container, bcast (PLAN.md, Phase 3).
#
# Usage:
#   sudo images/bcast/deploy.sh             create bcast if missing, else push config and restart
#   sudo images/bcast/deploy.sh --replace   recreate bcast from the latest chat-uses/bcast image
#                                           (the stream goes down for about a minute)
#   --image ALIAS                           launch from another image (default chat-uses/bcast)
#
# From the host it pushes these, or removes bcast's copy when the host has none:
#   /etc/chat-uses/bcast.local.env            setting overrides (see images/bcast/rootfs/etc/chat-uses/bcast.env.tmpl)
#   /etc/chat-uses/secrets/youtube-stream-key the YouTube stream key (root:bcast 0640 inside)
# The image itself never contains secrets.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

IMAGE=chat-uses/bcast
NAME=bcast
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

in_bcast() { incus exec "$NAME" --project infra -- "$@"; }

exists=0
incus info "$NAME" --project infra >/dev/null 2>&1 && exists=1
# On air or off (air.py) outlives a new container: a redeploy doesn't put the
# stream on YouTube, nor take it off.
air=$(mktemp)
trap 'rm -f "$air"' EXIT
if ((exists)); then
	in_bcast cat /var/lib/chat-bcast/control/air.json >"$air" 2>/dev/null || true
fi
if ((exists && REPLACE)); then
	log "Removing the old $NAME"
	incus delete --force "$NAME" --project infra >/dev/null
	exists=0
fi
if ((!exists)); then
	log "Launching $NAME from $IMAGE at $BCAST_IP"
	incus launch "$IMAGE" "$NAME" --project infra -p bcast --device "eth0,ipv4.address=$BCAST_IP" >/dev/null
	for _ in $(seq 1 60); do
		in_bcast true >/dev/null 2>&1 && break
		sleep 1
	done
fi

log "Configuration"
if [[ -f $CHAT_USES_ETC/bcast.local.env ]]; then
	incus file push "$CHAT_USES_ETC/bcast.local.env" "$NAME/etc/chat-uses/bcast.local.env" --project infra --mode 0644
	info "pushed bcast.local.env"
else
	in_bcast rm -f /etc/chat-uses/bcast.local.env
	info "no $CHAT_USES_ETC/bcast.local.env: image defaults"
fi
key=$CHAT_USES_ETC/secrets/youtube-stream-key
if [[ -s $key ]]; then
	incus file push "$key" "$NAME/etc/chat-uses/secrets/youtube-stream-key" --project infra --mode 0640
	in_bcast chown root:bcast /etc/chat-uses/secrets/youtube-stream-key
	info "pushed the YouTube stream key"
else
	in_bcast rm -f /etc/chat-uses/secrets/youtube-stream-key
	info "no stream key at $key: the forwarder stays idle"
fi

if [[ -s $air ]]; then
	incus file push "$air" "$NAME/var/lib/chat-bcast/control/air.json" --project infra --mode 0644
fi
log "Restarting the pipeline"
if wait_booted "$NAME" infra; then
	in_bcast systemctl restart chat-bcast.target
	sleep 5
	in_bcast systemctl --no-pager --plain list-units 'chat-bcast-*' | sed -n '1,12p' | sed 's/^/    /'
else
	warn "systemd is not running in $NAME; services were not restarted"
fi
info "$(in_bcast chat-bcast-air status 2>/dev/null || echo "on air or off: unknown (an older image)")"
info "check: images/bcast/check.sh; on air: infra/host/onair.sh on"
