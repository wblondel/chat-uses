#!/usr/bin/env bash
# Provision a Debian 13 root as the chat-uses intranet (PLAN.md 7.2, Phase 5):
# nginx serving a homepage and files (Freedoom), and kiwix-serve for the
# offline Wikipedia, which images/intranet/deploy.sh --zim installs.
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

# curl: deploy.sh --zim downloads inside the container.
PACKAGES=(nginx kiwix-tools freedoom curl ca-certificates tzdata procps systemd)

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

log "User and directories"
if ! id kiwix >/dev/null 2>&1; then
	useradd --system --home-dir /srv/zim --shell /usr/sbin/nologin kiwix
fi
install -d -m 0755 -o root -g root /srv/zim

log "Copying the chat-uses files"
cp -a "$STAGE/rootfs/." /
rm -f /etc/nginx/sites-enabled/default
ln -sf ../sites-available/intranet /etc/nginx/sites-enabled/intranet
# Freedoom's game data, from the Debian package, in the file cabinet.
install -d /var/www/intranet/files/freedoom
for wad in /usr/share/games/doom/freedoom1.wad /usr/share/games/doom/freedoom2.wad; do
	[[ -f $wad ]] || { echo "missing $wad (has the freedoom package moved it?)" >&2; exit 1; }
	ln -sf "$wad" "/var/www/intranet/files/freedoom/$(basename "$wad")"
done
nginx -t 2>&1 | sed 's/^/    /'
# sed reads it all: head would close the pipe early and, with pipefail, end the build.
kiwix-serve --version 2>&1 | sed -n '1,2s/^/    /p'

log "Services"
systemctl enable nginx.service chat-kiwix.service >/dev/null 2>&1

log "Cleaning up"
apt-get autoremove -y -q >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/* "$STAGE" /root/.bash_history
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +

log "Done"
