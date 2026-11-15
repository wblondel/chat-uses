#!/usr/bin/env bash
# Provision a Debian 13 root as the chat-uses egress proxy (PLAN.md 7.2,
# Phase 5): Squid with a domain allowlist, refusing private destinations,
# with a shared bandwidth cap.
#
# Runs INSIDE the image being built (images/build.sh pushes it into a builder
# container), as root. Expects the rendered files in $STAGE/rootfs.
#
# Environment: STAGE, CHAT_TIMEZONE, BUILD_PROXY (see images/build.sh)

set -euo pipefail

STAGE=${STAGE:-/tmp/chat-uses-build}
CHAT_TIMEZONE=${CHAT_TIMEZONE:-UTC}
export DEBIAN_FRONTEND=noninteractive

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

PACKAGES=(squid logrotate ca-certificates tzdata procps systemd)

proxy=()
if [[ -n ${BUILD_PROXY:-} ]]; then
	proxy=(-o "Acquire::http::Proxy=$BUILD_PROXY" -o "Acquire::https::Proxy=$BUILD_PROXY")
fi

log "Installing packages"
apt-get "${proxy[@]}" update -qq
apt-get "${proxy[@]}" install -y -q --no-install-recommends "${PACKAGES[@]}" >/dev/null

log "Timezone $CHAT_TIMEZONE"
ln -sf "/usr/share/zoneinfo/$CHAT_TIMEZONE" /etc/localtime
printf '%s\n' "$CHAT_TIMEZONE" >/etc/timezone

log "Copying the chat-uses files"
install -d -m 0755 /etc/squid/conf.d
cp -a "$STAGE/rootfs/." /
# Debian's own snippets would loosen the rules above.
rm -f /etc/squid/conf.d/debian.conf
squid -k parse -f /etc/squid/squid.conf 2>&1 | grep -iE "error|warning" | sed 's/^/    /' || true
squid -k parse -f /etc/squid/squid.conf >/dev/null 2>&1 || { echo "squid.conf doesn't parse" >&2; exit 1; }

log "Services"
systemctl enable squid.service >/dev/null 2>&1

log "Cleaning up"
apt-get autoremove -y -q >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/* "$STAGE" /root/.bash_history
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +

log "Done"
