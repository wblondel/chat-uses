#!/usr/bin/env bash
# Deploy the control plane container, brain (PLAN.md, Phase 4).
#
# Usage:
#   sudo images/brain/deploy.sh             create brain if missing, else push config and restart
#   sudo images/brain/deploy.sh --replace   recreate brain from the latest chat-uses/brain image
#                                           (its database starts empty: nothing in it matters yet)
#   --image ALIAS                           launch from another image (default chat-uses/brain)
#
# From the host it pushes these, or removes brain's copy when the host has none:
#   /etc/chat-uses/brain.local.env             setting overrides (see images/brain/rootfs/etc/chat-uses/brain.env.tmpl)
#   /etc/chat-uses/secrets/discord-bot-token   the Discord bot token          (root:brain 0640 inside)
#   /etc/chat-uses/secrets/google-api-key      the YouTube Data API key       (root:brain 0640 inside)
# The image itself never contains secrets.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

IMAGE=chat-uses/brain
NAME=brain
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

in_brain() { incus exec "$NAME" --project infra -- "$@"; }

exists=0
incus info "$NAME" --project infra >/dev/null 2>&1 && exists=1
if ((exists && REPLACE)); then
	log "Removing the old $NAME"
	incus delete --force "$NAME" --project infra >/dev/null
	exists=0
fi
if ((!exists)); then
	log "Launching $NAME from $IMAGE at $BRAIN_IP"
	incus launch "$IMAGE" "$NAME" --project infra -p brain --device "eth0,ipv4.address=$BRAIN_IP" >/dev/null
	for _ in $(seq 1 60); do
		in_brain true >/dev/null 2>&1 && break
		sleep 1
	done
fi

log "Configuration"
if [[ -f $CHAT_USES_ETC/brain.local.env ]]; then
	incus file push "$CHAT_USES_ETC/brain.local.env" "$NAME/etc/chat-uses/brain.local.env" --project infra --mode 0644
	info "pushed brain.local.env"
else
	in_brain rm -f /etc/chat-uses/brain.local.env
	info "no $CHAT_USES_ETC/brain.local.env: image defaults (target $TARGET_IP)"
fi
in_brain install -d -m 0750 -o root -g brain /etc/chat-uses/secrets # images before step 4 lack it
for secret in discord-bot-token google-api-key; do
	src=$CHAT_USES_ETC/secrets/$secret
	if [[ -s $src ]]; then
		incus file push "$src" "$NAME/etc/chat-uses/secrets/$secret" --project infra --mode 0640
		in_brain chown root:brain "/etc/chat-uses/secrets/$secret"
		info "pushed $secret"
	else
		in_brain rm -f "/etc/chat-uses/secrets/$secret"
		info "no $src: that chat adapter stays off"
	fi
done

log "Restarting brain"
if in_brain test -d /run/systemd/system; then
	in_brain systemctl restart chat-brain.service
	sleep 3
	in_brain systemctl --no-pager --plain list-units 'chat-brain*' | sed -n '1,3p' | sed 's/^/    /'
else
	warn "systemd is not running in $NAME; the service was not restarted"
fi
info "check: images/brain/check.sh"
