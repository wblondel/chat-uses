#!/usr/bin/env bash
# Provision a Debian 13 root as the chat-uses Xfce desktop (PLAN.md, Phase 2).
#
# Runs INSIDE the image being built (build.sh pushes it into a builder
# container), as root. Expects the rendered overlay in $STAGE/rootfs.
#
# Environment:
#   STAGE          staging directory (default /tmp/chat-uses-build)
#   CHAT_TIMEZONE  e.g. Europe/Paris
#   CHAT_LOCALE    default en_US.UTF-8
#   BUILD_PROXY    optional HTTP proxy for apt during the build only

set -euo pipefail

STAGE=${STAGE:-/tmp/chat-uses-build}
CHAT_TIMEZONE=${CHAT_TIMEZONE:-UTC}
CHAT_LOCALE=${CHAT_LOCALE:-en_US.UTF-8}
export DEBIAN_FRONTEND=noninteractive

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

PACKAGES=(
	# Desktop
	xfce4 xfce4-terminal mousepad dbus-x11 at-spi2-core
	# Display server: TigerVNC is the X server
	tigervnc-standalone-server xfonts-base x11-xserver-utils
	# Audio
	pulseaudio pulseaudio-utils
	# Apps and fonts
	firefox-esr fonts-dejavu-core fonts-noto-color-emoji librsvg2-common
	# Basics chat will want
	sudo locales tzdata ca-certificates procps psmisc less nano curl
	# Already in images:debian; listed so systemctl exists on any base
	systemd
)
BUILD_ONLY=(librsvg2-bin)

# The build proxy is passed per command, so nothing of it stays in the image.
proxy=()
if [[ -n ${BUILD_PROXY:-} ]]; then
	proxy=(-o "Acquire::http::Proxy=$BUILD_PROXY" -o "Acquire::https::Proxy=$BUILD_PROXY")
fi

log "Installing packages"
apt-get "${proxy[@]}" update -qq
apt-get "${proxy[@]}" install -y -q --no-install-recommends "${PACKAGES[@]}" "${BUILD_ONLY[@]}" >/dev/null

log "User chat (uid 1000, passwordless sudo)"
if ! id chat >/dev/null 2>&1; then
	useradd --create-home --uid 1000 --shell /bin/bash --groups sudo,audio,video chat
fi
passwd --lock chat >/dev/null

log "Locale $CHAT_LOCALE, timezone $CHAT_TIMEZONE"
sed -i "s/^# *\(${CHAT_LOCALE//./\\.}\)/\1/" /etc/locale.gen
locale-gen >/dev/null
update-locale LANG="$CHAT_LOCALE"
ln -sf "/usr/share/zoneinfo/$CHAT_TIMEZONE" /etc/localtime
printf '%s\n' "$CHAT_TIMEZONE" >/etc/timezone

log "Copying the chat-uses files"
cp -a "$STAGE/rootfs/." /
chown -R chat:chat /home/chat
chmod 0440 /etc/sudoers.d/chat
visudo -cf /etc/sudoers.d/chat >/dev/null

# Xfce settings for chat live in /home/chat/.config (layered over the package
# defaults, which must stay intact: they define the failsafe session).
# Panel: a system-wide default means no "first start of the panel" dialog.
cp /etc/xdg/xfce4/panel/default.xml /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml

rsvg-convert -w 1600 -h 900 -o /usr/share/chat-uses/wallpaper.png /usr/share/chat-uses/wallpaper.svg

log "Services"
systemctl enable chat-xvnc.service chat-audio.service chat-session.service >/dev/null 2>&1

log "Cleaning up"
apt-get purge -y -q "${BUILD_ONLY[@]}" >/dev/null
apt-get autoremove -y -q >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/* "$STAGE" /root/.bash_history
# Every instance made from the image must get its own machine ID.
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +

log "Done"
