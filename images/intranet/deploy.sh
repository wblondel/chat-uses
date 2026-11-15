#!/usr/bin/env bash
# Deploy the intranet container, intranet (PLAN.md 7.2, Phase 5).
#
# Usage:
#   sudo images/intranet/deploy.sh                 create intranet if missing, else restart its services
#   sudo images/intranet/deploy.sh --replace       recreate it from the latest chat-uses/intranet image
#   --image ALIAS                                  launch from another image (default chat-uses/intranet)
#   --zim URL                                      download this offline Wikipedia into it (replacing the
#                                                  one there), checked against URL.sha256 when Kiwix has one
#   --zim-file PATH                                the same from a file on the host
#
# ZIM files live on their own volume (intranet-zim, mounted at /srv/zim), so
# --replace keeps them. Pick one at https://download.kiwix.org/zim/wikipedia/
# (docs/phase5.md says which).

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

IMAGE=chat-uses/intranet
NAME=intranet
VOLUME=intranet-zim
REPLACE=0
ZIM_URL=""
ZIM_FILE=""
while (($#)); do
	case $1 in
	--replace) REPLACE=1 ;;
	--image) IMAGE=${2:?}; shift ;;
	--zim) ZIM_URL=${2:?}; shift ;;
	--zim-file) ZIM_FILE=${2:?}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
load_config
[[ -z $ZIM_URL || $ZIM_URL =~ ^https://[^[:space:]]+\.zim$ ]] || die "--zim takes an https:// URL of a .zim file"
[[ -z $ZIM_FILE || -f $ZIM_FILE ]] || die "no such file: $ZIM_FILE"

in_intranet() { incus exec "$NAME" --project infra -- "$@"; }

exists=0
incus info "$NAME" --project infra >/dev/null 2>&1 && exists=1
if ((exists && REPLACE)); then
	log "Removing the old $NAME (its ZIM volume stays)"
	incus delete --force "$NAME" --project infra >/dev/null
	exists=0
fi
if ! incus storage volume show "$POOL_NAME" "$VOLUME" --project infra >/dev/null 2>&1; then
	log "Creating the ZIM volume $VOLUME ($DISK_INTRANET)"
	incus storage volume create "$POOL_NAME" "$VOLUME" --project infra size="$DISK_INTRANET" >/dev/null
fi
if ((!exists)); then
	log "Launching $NAME from $IMAGE at $INTRANET_IP"
	incus init "$IMAGE" "$NAME" --project infra -p intranet --device "eth0,ipv4.address=$INTRANET_IP" >/dev/null
	incus config device add "$NAME" zim disk pool="$POOL_NAME" source="$VOLUME" path=/srv/zim --project infra >/dev/null
	incus start "$NAME" --project infra
	for _ in $(seq 1 60); do
		in_intranet true >/dev/null 2>&1 && break
		sleep 1
	done
fi
# A new volume's root is 0711: kiwix-serve's own user must be able to list it.
in_intranet chmod 0755 /srv/zim

if [[ -n $ZIM_URL ]]; then
	name=$(basename "$ZIM_URL")
	log "Downloading $name into $NAME (this can take a while)"
	# Inside the container: infra has the internet, and the host's disk isn't used.
	in_intranet curl -fL --retry 3 --progress-bar -o "/srv/zim/.download" "$ZIM_URL"
	if sums=$(in_intranet curl -fsSL "$ZIM_URL.sha256" 2>/dev/null); then
		expected=${sums%% *}
		actual=$(in_intranet sha256sum /srv/zim/.download | awk '{print $1}')
		[[ $expected == "$actual" ]] || { in_intranet rm -f /srv/zim/.download; die "checksum mismatch for $name"; }
		info "checksum matches Kiwix's $name.sha256"
	else
		warn "no $name.sha256 next to it: not checked"
	fi
	in_intranet sh -c "rm -f /srv/zim/*.zim && mv /srv/zim/.download '/srv/zim/$name' && chmod 0644 '/srv/zim/$name'"
elif [[ -n $ZIM_FILE ]]; then
	name=$(basename "$ZIM_FILE")
	log "Copying $name into $NAME"
	in_intranet sh -c 'rm -f /srv/zim/*.zim'
	incus file push "$ZIM_FILE" "$NAME/srv/zim/$name" --project infra --mode 0644
fi
info "offline Wikipedia: $(in_intranet sh -c 'ls /srv/zim/*.zim 2>/dev/null | xargs -r -n1 basename' | tr '\n' ' ' || true)"

log "Restarting the intranet's services"
if in_intranet test -d /run/systemd/system; then
	in_intranet systemctl restart nginx.service chat-kiwix.service
	sleep 2
	in_intranet systemctl --no-pager --plain list-units nginx.service chat-kiwix.service | sed -n '1,3p' | sed 's/^/    /'
else
	warn "systemd is not running in $NAME; services were not restarted"
fi
info "check: images/intranet/check.sh"
