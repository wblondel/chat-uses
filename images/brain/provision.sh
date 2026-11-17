#!/usr/bin/env bash
# Provision a Debian 13 root as the chat-uses control plane, brain (PLAN.md,
# Phase 4): the Python service that turns chat into desktop input and feeds
# the overlay.
#
# Runs INSIDE the image being built (images/build.sh pushes it into a builder
# container), as root. Expects the rendered files in $STAGE/rootfs, among
# them the code (control/src/chatuses) and its locked, hash-pinned
# dependencies (control/requirements.txt).
#
# Environment: STAGE, CHAT_TIMEZONE, BUILD_PROXY (see images/build.sh)

set -euo pipefail

STAGE=${STAGE:-/tmp/chat-uses-build}
CHAT_TIMEZONE=${CHAT_TIMEZONE:-UTC}
VENV=/opt/chat-brain/venv
export DEBIAN_FRONTEND=noninteractive

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

PACKAGES=(python3 python3-venv ca-certificates tzdata procps systemd
	# Screen OCR (PLAN.md 7.4): the word lists' languages.
	tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-spa tesseract-ocr-ita tesseract-ocr-ara)

proxy=()
if [[ -n ${BUILD_PROXY:-} ]]; then
	proxy=(-o "Acquire::http::Proxy=$BUILD_PROXY" -o "Acquire::https::Proxy=$BUILD_PROXY")
fi

log "Installing packages"
apt-get "${proxy[@]}" update -qq
apt-get "${proxy[@]}" install -y -q --no-install-recommends "${PACKAGES[@]}" >/dev/null

log "Python dependencies (locked, hash-checked, wheels only)"
python3 -m venv "$VENV"
# --require-hashes: every file must match uv.lock. --only-binary: nothing is
# compiled here. The system's CA store, not pip's own copy.
"$VENV/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check \
	--require-hashes --only-binary :all: --no-deps \
	--cert /etc/ssl/certs/ca-certificates.crt ${BUILD_PROXY:+--proxy "$BUILD_PROXY"} \
	-r "$STAGE/rootfs/usr/lib/chat-brain/requirements.txt"
"$VENV/bin/pip" list --format freeze --disable-pip-version-check | sed 's/^/    /'

log "User and directories"
if ! id brain >/dev/null 2>&1; then
	useradd --system --home-dir /var/lib/chat-brain --create-home --shell /usr/sbin/nologin brain
fi
install -d -m 0750 -o brain -g brain /var/lib/chat-brain
# deploy.sh puts the Discord token and the Google API key here (root:brain 0640).
install -d -m 0750 -o root -g brain /etc/chat-uses/secrets

log "Timezone $CHAT_TIMEZONE"
ln -sf "/usr/share/zoneinfo/$CHAT_TIMEZONE" /etc/localtime
printf '%s\n' "$CHAT_TIMEZONE" >/etc/timezone

log "Copying the chat-uses files"
cp -a "$STAGE/rootfs/." /
find /usr/lib/chat-brain -name __pycache__ -prune -exec rm -rf {} +
"$VENV/bin/python" -m compileall -q /usr/lib/chat-brain/chatuses
# Smoke test: the code imports with the installed dependencies, tesseract runs.
PYTHONPATH=/usr/lib/chat-brain "$VENV/bin/python" -c 'import chatuses.brain, chatuses.console, chatuses.admin'
PYTHONPATH=/usr/lib/chat-brain "$VENV/bin/python" -c 'from pathlib import Path
from chatuses.referee import laws
laws.load(Path("/usr/lib/chat-brain/rulesets"), "roguelike")' # PyYAML, and a valid default ruleset
PYTHONPATH=/usr/lib/chat-brain "$VENV/bin/python" -c 'from pathlib import Path
from chatuses.game import achievements
found = achievements.load(Path("/usr/lib/chat-brain/achievements"))
print(f"    achievements: {len(found)}")' # every achievements/*.yaml is valid
tesseract --list-langs 2>/dev/null | sed -n '2,$p' | tr '\n' ' ' | sed 's/^/    tesseract languages: /; s/$/\n/'

log "Services"
systemctl enable chat-brain.service >/dev/null 2>&1

log "Cleaning up"
apt-get autoremove -y -q >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/* "$STAGE" /root/.bash_history
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +

log "Done"
