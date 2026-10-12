# shellcheck shell=bash
# Shared helpers for chat-uses host scripts. Source it; don't run it.

# shellcheck disable=SC2034 # used by the scripts that source this file
CHAT_USES_REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CHAT_USES_HOST_DIR="$CHAT_USES_REPO/infra/host"
CHAT_USES_INCUS_DIR="$CHAT_USES_REPO/infra/incus"
CHAT_USES_ETC=/etc/chat-uses
CHAT_USES_CONFIG=${CHAT_USES_CONFIG:-$CHAT_USES_ETC/host.env}
WG_DIR=/etc/wireguard
WG_PEERS_DIR=$WG_DIR/chat-uses-peers

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

have_systemd() { [[ -d /run/systemd/system ]]; }

# Variable names that templates may use: everything defined in the example
# config, plus values derived at runtime.
config_var_names() {
	grep -oE '^[A-Z][A-Z0-9_]*=' "$CHAT_USES_HOST_DIR/host.env.example" | tr -d '='
	printf '%s\n' PUBLIC_HTTPS_RULE
}

# Load defaults, then the host's config, detect what was left empty and
# export everything for envsubst.
load_config() {
	set -a
	# shellcheck source=infra/host/host.env.example
	source "$CHAT_USES_HOST_DIR/host.env.example"
	if [[ -f $CHAT_USES_CONFIG ]]; then
		# shellcheck disable=SC1090
		source "$CHAT_USES_CONFIG"
	fi
	set +a

	if [[ -z $WAN_IF ]]; then
		WAN_IF=$(ip -o route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
	fi
	if [[ -z $WG_ENDPOINT && -n $WAN_IF ]]; then
		WG_ENDPOINT=$(ip -4 -o addr show dev "$WAN_IF" scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]; exit}')
	fi
	if [[ $PUBLIC_HTTPS == 1 ]]; then
		PUBLIC_HTTPS_RULE='tcp dport { 80, 443 } accept'
	else
		PUBLIC_HTTPS_RULE='# Ports 80/443 closed (PUBLIC_HTTPS=0)'
	fi
	export WAN_IF WG_ENDPOINT PUBLIC_HTTPS_RULE
}

# render TEMPLATE: print the template with ${VARS} from the config filled in.
# Fails if a placeholder is left over (typo or missing variable).
render() {
	local template=$1 format out leftover
	format=$(config_var_names | sed 's/.*/${&}/' | tr '\n' ' ')
	out=$(envsubst "$format" <"$template")
	leftover=$(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' <<<"$out" | sort -u | tr '\n' ' ' || true)
	[[ -z $leftover ]] || die "$template: unknown placeholders: $leftover"
	printf '%s\n' "$out"
}

# write_file DEST MODE: write stdin to DEST atomically if the content changed.
# Returns 0 if the file changed, 1 if it was already up to date.
write_file() {
	local dest=$1 mode=$2 tmp
	tmp=$(mktemp)
	cat >"$tmp"
	if [[ -f $dest ]] && cmp -s "$tmp" "$dest"; then
		rm -f "$tmp"
		return 1
	fi
	install -D -m "$mode" "$tmp" "$dest"
	rm -f "$tmp"
	return 0
}

# --- WireGuard -----------------------------------------------------------------

wg_server_pubkey() { cat "$WG_DIR/chat-uses-server.pub"; }

# Rebuild wg0.conf from the server key and the peer files, then apply it to
# the running interface without dropping existing sessions.
wg_render_config() {
	local conf="$WG_DIR/$WG_IF.conf" changed=0
	{
		printf '# Managed by chat-uses (infra/host/bootstrap.sh, wg-add-peer.sh).\n'
		printf '# Edit peers in %s/, not here.\n\n' "$WG_PEERS_DIR"
		printf '[Interface]\nAddress = %s/%s\nListenPort = %s\nPrivateKey = %s\n' \
			"$WG_HOST_IP" "$WG_PREFIX" "$WG_PORT" "$(cat "$WG_DIR/chat-uses-server.key")"
		local peer
		for peer in "$WG_PEERS_DIR"/*.conf; do
			[[ -e $peer ]] || continue
			printf '\n'
			cat "$peer"
		done
	} | write_file "$conf" 0600 && changed=1

	if ip link show "$WG_IF" >/dev/null 2>&1; then
		if ((changed)); then
			wg syncconf "$WG_IF" <(wg-quick strip "$WG_IF")
			info "WireGuard peers reloaded"
		fi
	elif have_systemd; then
		systemctl enable --now "wg-quick@$WG_IF" >/dev/null
		info "WireGuard interface $WG_IF started"
	else
		warn "no systemd: start the tunnel with 'wg-quick up $WG_IF'"
	fi
}
