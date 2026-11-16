#!/usr/bin/env bash
# Deploy the control plane container, brain (PLAN.md, Phase 4).
#
# Usage:
#   sudo images/brain/deploy.sh             create brain if missing, else push config and restart
#   sudo images/brain/deploy.sh --replace   recreate brain from the latest chat-uses/brain image
#   --image ALIAS                           launch from another image (default chat-uses/brain)
#
# brain's store (bans, timeouts, strikes, the video it follows, the event log,
# OCR screenshots) is on its own volume, brain-data, which outlives the
# container: --replace keeps it (PLAN.md D53). A brain from before that keeps
# its store on its own disk; the first deploy moves it to the volume.
#
# From the host it pushes these, or removes brain's copy when the host has none:
#   /etc/chat-uses/brain.local.env             setting overrides (see images/brain/rootfs/etc/chat-uses/brain.env.tmpl)
#   /etc/chat-uses/secrets/discord-bot-token   the Discord bot token          (root:brain 0640 inside)
#   /etc/chat-uses/secrets/google-api-key      the YouTube Data API key       (root:brain 0640 inside)
#   /etc/chat-uses/secrets/brain-incus.crt,    brain's Incus client certificate, and the host's to
#     brain-incus.key, incus-server.crt        pin (bootstrap.sh incus-api makes them): snapshots
#   /etc/chat-uses/secrets/brain-admin-token   the admin dashboard's token; made here if missing
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
wait_exec() {
	for _ in $(seq 1 60); do
		in_brain true >/dev/null 2>&1 && return 0
		sleep 1
	done
	die "$NAME doesn't answer"
}
VOLUME=brain-data
DATA=/var/lib/chat-brain
has_volume_attached() { incus config device get "$NAME" data source --project infra >/dev/null 2>&1; }

if ! incus storage volume show "$POOL_NAME" "$VOLUME" --project infra >/dev/null 2>&1; then
	log "Creating brain's data volume $VOLUME (${DISK_BRAIN_DATA:-10GiB})"
	incus storage volume create "$POOL_NAME" "$VOLUME" --project infra size="${DISK_BRAIN_DATA:-10GiB}" >/dev/null
fi

exists=0
incus info "$NAME" --project infra >/dev/null 2>&1 && exists=1
# Before the volume, the store was on brain's own disk: move it over (before a
# --replace deletes that disk), unless the volume already holds a store.
if ((exists)) && ! has_volume_attached; then
	log "Moving brain's store to the volume $VOLUME"
	[[ $(incus list "$NAME" --project infra --format csv -c s) == RUNNING ]] || incus start "$NAME" --project infra
	wait_exec
	in_brain systemctl stop chat-brain.service 2>/dev/null || true
	incus config device add "$NAME" data-move disk pool="$POOL_NAME" source="$VOLUME" path=/mnt/brain-data --project infra >/dev/null
	if in_brain test -e /mnt/brain-data/brain.db; then
		warn "the volume already holds a store: keeping it, not the one on $NAME's disk"
	else
		in_brain cp -a "$DATA/." /mnt/brain-data/
		info "moved $(in_brain du -sh "$DATA" | cut -f1)"
	fi
	incus config device remove "$NAME" data-move --project infra >/dev/null
	incus config device add "$NAME" data disk pool="$POOL_NAME" source="$VOLUME" path="$DATA" --project infra >/dev/null
fi
if ((exists && REPLACE)); then
	log "Removing the old $NAME (its data volume stays)"
	incus delete --force "$NAME" --project infra >/dev/null
	exists=0
fi
if ((!exists)); then
	log "Launching $NAME from $IMAGE at $BRAIN_IP"
	incus init "$IMAGE" "$NAME" --project infra -p brain --device "eth0,ipv4.address=$BRAIN_IP" >/dev/null
	incus config device add "$NAME" data disk pool="$POOL_NAME" source="$VOLUME" path="$DATA" --project infra >/dev/null
	incus start "$NAME" --project infra
	wait_exec
fi
# The volume's files belong to brain's user, whose number may change between
# images; a new volume's root belongs to root.
in_brain chown -R brain:brain "$DATA"
in_brain chmod 0750 "$DATA"

log "Configuration"
if [[ -f $CHAT_USES_ETC/brain.local.env ]]; then
	incus file push "$CHAT_USES_ETC/brain.local.env" "$NAME/etc/chat-uses/brain.local.env" --project infra --mode 0644
	info "pushed brain.local.env"
else
	in_brain rm -f /etc/chat-uses/brain.local.env
	info "no $CHAT_USES_ETC/brain.local.env: image defaults (target $TARGET_IP)"
fi
in_brain install -d -m 0750 -o root -g brain /etc/chat-uses/secrets # images before step 4 lack it
install -d -m 0700 "$CHAT_USES_ETC/secrets"
if [[ ! -s $CHAT_USES_ETC/secrets/brain-admin-token ]]; then
	(umask 077 && openssl rand -hex 32 >"$CHAT_USES_ETC/secrets/brain-admin-token")
	info "made the admin dashboard's token: $CHAT_USES_ETC/secrets/brain-admin-token"
fi
# host file -> brain's name for it -> what's off without it
for secret in \
	"discord-bot-token discord-bot-token Discord" \
	"google-api-key google-api-key YouTube chat" \
	"brain-incus.crt incus-client.crt forensic snapshots (bootstrap.sh incus-api)" \
	"brain-incus.key incus-client.key forensic snapshots" \
	"incus-server.crt incus-server.crt forensic snapshots" \
	"brain-admin-token admin-token the admin dashboard"; do
	read -r src dest what <<<"$secret"
	src=$CHAT_USES_ETC/secrets/$src
	if [[ -s $src ]]; then
		incus file push "$src" "$NAME/etc/chat-uses/secrets/$dest" --project infra --mode 0640
		in_brain chown root:brain "/etc/chat-uses/secrets/$dest"
		info "pushed $dest"
	else
		in_brain rm -f "/etc/chat-uses/secrets/$dest"
		info "no $src: $what stays off"
	fi
done

log "Restarting brain"
if wait_booted "$NAME" infra; then
	in_brain systemctl restart chat-brain.service
	sleep 3
	in_brain systemctl --no-pager --plain list-units 'chat-brain*' | sed -n '1,3p' | sed 's/^/    /'
else
	warn "systemd is not running in $NAME; the service was not restarted"
fi
info "check: images/brain/check.sh"
