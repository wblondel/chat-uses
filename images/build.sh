#!/usr/bin/env bash
# Build a chat-uses Incus image from images/<name>/ (PLAN.md, Phases 2-3).
#
# Launches a throwaway builder container in project "infra" (it needs the
# internet for packages), copies images/<name>/rootfs/ in, runs
# images/<name>/provision.sh, and publishes the result as chat-uses/<name>.
#
# Usage:
#   sudo images/build.sh <name> [--base IMAGE] [--alias NAME] [--proxy URL] [--keep]
#
#   <name>        debian-xfce (the target desktop), bcast (the broadcaster) or
#                 brain (the control plane)
#   --base IMAGE  base image (default images:debian/13)
#   --alias NAME  alias to publish (default chat-uses/<name>). The image also
#                 gets a dated alias NAME/YYYYmmdd-HHMM, so older builds stay
#                 available for rollback.
#   --proxy URL   HTTP proxy for apt during the build only
#   --keep        keep the stopped builder container afterwards
#
# Environment variables named BUILD_* are passed into the builder, for
# image-specific options (e.g. BUILD_MEDIAMTX_URL for bcast).
#
# Files under rootfs/ are copied into the image (symlinks are followed, so an
# image can include a directory from elsewhere in the repo); *.tmpl files are
# first filled in with values from host.env, like the infra/ templates.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../infra/host/lib.sh"

NAME=${1:-}
if [[ $NAME == -h || $NAME == --help ]]; then
	sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
	exit 0
fi
[[ $NAME =~ ^[a-z0-9-]+$ ]] || die "usage: $0 <image-name> [options] (see --help)"
shift
IMAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$NAME"
[[ -f $IMAGE_DIR/provision.sh ]] || die "no image called '$NAME' (expected $IMAGE_DIR/provision.sh)"
BASE=images:debian/13
ALIAS=chat-uses/$NAME
PROXY=""
KEEP=0
while (($#)); do
	case $1 in
	--base) BASE=${2:?--base needs a value}; shift ;;
	--alias) ALIAS=${2:?--alias needs a value}; shift ;;
	--proxy) PROXY=${2:?--proxy needs a value}; shift ;;
	--keep) KEEP=1 ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done

[[ $EUID -eq 0 ]] || die "run as root"
load_config
incus profile show builder --project infra >/dev/null 2>&1 ||
	die "profile infra/builder missing: run infra/host/bootstrap.sh profiles"

stamp=$(date -u +%Y%m%d-%H%M)
builder=build-$NAME-$stamp
stage=$(mktemp -d)
commit=$(git -C "$CHAT_USES_REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)

cleanup() {
	rm -rf "$stage"
	if ((KEEP)); then
		info "builder kept: $builder (project infra)"
	else
		incus delete --force "$builder" --project infra >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT

log "Rendering files (commit $commit)"
cp -aL "$IMAGE_DIR/rootfs" "$stage/rootfs"
while IFS= read -r -d '' tmpl; do
	render "$tmpl" >"${tmpl%.tmpl}"
	rm "$tmpl"
done < <(find "$stage/rootfs" -name '*.tmpl' -print0)
cp "$IMAGE_DIR/provision.sh" "$stage/"

log "Builder $builder from $BASE"
incus launch "$BASE" "$builder" --project infra -p builder >/dev/null
for _ in $(seq 1 60); do
	incus list "$builder" --project infra --format csv -c 4 | grep -q '[0-9]' && break
	sleep 1
done
incus list "$builder" --project infra --format csv -c 4 | grep -q '[0-9]' ||
	die "builder got no IPv4 address on $INFRANET_NAME"

log "Provisioning (this takes a few minutes)"
tar -C "$stage" -cf - . | incus exec "$builder" --project infra -- \
	sh -c 'mkdir -p /tmp/chat-uses-build && tar -C /tmp/chat-uses-build -xf -'
build_env=(--env STAGE=/tmp/chat-uses-build --env CHAT_TIMEZONE="$TIMEZONE" --env BUILD_PROXY="$PROXY")
while IFS= read -r var; do
	[[ $var == BUILD_PROXY ]] || build_env+=(--env "$var=${!var}")
done < <(compgen -e | grep '^BUILD_' || true)
incus exec "$builder" --project infra "${build_env[@]}" -- bash /tmp/chat-uses-build/provision.sh

log "Publishing"
incus stop "$builder" --project infra --timeout 60 2>/dev/null ||
	incus stop "$builder" --project infra --force
incus publish "$builder" --project infra --alias "$ALIAS/$stamp" --compression none \
	description="chat-uses $NAME ($stamp, $commit)" \
	os=debian release=trixie variant="$NAME" chat-uses.commit="$commit" >/dev/null
fingerprint=$(incus image info "$ALIAS/$stamp" --project infra | awk '/^Fingerprint:/ {print $2}')
incus image alias delete "$ALIAS" --project infra >/dev/null 2>&1 || true
incus image alias create "$ALIAS" "$fingerprint" --project infra

log "Published"
info "image:   ${fingerprint:0:12} ($(incus image info "$ALIAS" --project infra | awk -F': ' '/^Size:/ {print $2}'))"
info "aliases: $ALIAS (latest), $ALIAS/$stamp"
info "check:   images/$NAME/check.sh"
