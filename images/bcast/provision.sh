#!/usr/bin/env bash
# Provision a Debian 13 root as the chat-uses broadcaster, bcast (PLAN.md,
# Phase 3): virtual display + kiosk browser showing the layout page, a
# PulseAudio mixer fed by the target's sound, an x264 encoder into MediaMTX,
# and a forwarder to YouTube.
#
# Runs INSIDE the image being built (images/build.sh pushes it into a builder
# container), as root. Expects the rendered overlay in $STAGE/rootfs.
#
# Environment: STAGE, CHAT_TIMEZONE, BUILD_PROXY (see images/build.sh), and
#   BUILD_MEDIAMTX_URL  download MediaMTX from this mirror instead of GitHub
#                       (fetched directly, not through BUILD_PROXY; the pinned
#                       SHA-256 must still match)

set -euo pipefail

STAGE=${STAGE:-/tmp/chat-uses-build}
CHAT_TIMEZONE=${CHAT_TIMEZONE:-UTC}
export DEBIAN_FRONTEND=noninteractive

# MediaMTX isn't packaged in Debian: pinned release, checked against the
# SHA-256 published with it (github.com/bluenviron/mediamtx/releases).
MEDIAMTX_VERSION=v1.21.3
MEDIAMTX_SHA256=0a2ea7eba96023631efa1bea7804823a9145b043b6646a8a05798d4ac24ed635

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

PACKAGES=(
	# Display and browser
	xvfb chromium fonts-dejavu-core fonts-noto-color-emoji
	# Audio, encoding, VNC relay and its web client
	pulseaudio pulseaudio-utils ffmpeg websockify novnc
	# Basics (python3 also runs chat-bcast-scene)
	ca-certificates curl procps tzdata python3 systemd
)

proxy=()
if [[ -n ${BUILD_PROXY:-} ]]; then
	proxy=(-o "Acquire::http::Proxy=$BUILD_PROXY" -o "Acquire::https::Proxy=$BUILD_PROXY")
fi

log "Installing packages"
apt-get "${proxy[@]}" update -qq
apt-get "${proxy[@]}" install -y -q --no-install-recommends "${PACKAGES[@]}" >/dev/null

log "MediaMTX $MEDIAMTX_VERSION"
tarball=/tmp/mediamtx.tar.gz
if [[ -n ${BUILD_MEDIAMTX_URL:-} ]]; then
	curl -fsSL --noproxy '*' -o "$tarball" "$BUILD_MEDIAMTX_URL"
else
	curl -fsSL ${BUILD_PROXY:+--proxy "$BUILD_PROXY"} -o "$tarball" \
		"https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VERSION/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz"
fi
echo "$MEDIAMTX_SHA256  $tarball" | sha256sum -c --quiet -
tar -xzf "$tarball" -C /usr/local/bin mediamtx
chmod 0755 /usr/local/bin/mediamtx
rm -f "$tarball"

log "User bcast"
if ! id bcast >/dev/null 2>&1; then
	useradd --system --home-dir /var/lib/chat-bcast --create-home --shell /usr/sbin/nologin bcast
fi

log "Timezone $CHAT_TIMEZONE"
ln -sf "/usr/share/zoneinfo/$CHAT_TIMEZONE" /etc/localtime
printf '%s\n' "$CHAT_TIMEZONE" >/etc/timezone

log "Copying the chat-uses files"
cp -a "$STAGE/rootfs/." /
# The web root: the layout page (from overlay/), noVNC, and a writable
# control/ folder for the scene and the page's config.
ln -sfn /usr/share/novnc /usr/share/chat-bcast/www/novnc
install -d -o bcast -g bcast /var/lib/chat-bcast/control
ln -sfn /var/lib/chat-bcast/control /usr/share/chat-bcast/www/control
printf '{"scene": "live"}\n' >/var/lib/chat-bcast/control/scene.json
printf '{"panic": false}\n' >/var/lib/chat-bcast/control/panic.json
chown bcast:bcast /var/lib/chat-bcast/control/scene.json /var/lib/chat-bcast/control/panic.json
# deploy.sh puts the YouTube stream key here (root:bcast 0640).
install -d -m 0750 -o root -g bcast /etc/chat-uses/secrets

log "Services"
systemctl enable chat-bcast.target chat-bcast-xvfb.service chat-bcast-audio.service \
	chat-bcast-mediamtx.service chat-bcast-websockify.service chat-bcast-audio-in.service \
	chat-bcast-browser.service chat-bcast-encoder.service chat-bcast-forwarder.service \
	chat-bcast-panic.service >/dev/null 2>&1

log "Cleaning up"
apt-get autoremove -y -q >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/* "$STAGE" /root/.bash_history
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +

log "Done"
