#!/usr/bin/env bash
# Add a device to the chat-uses WireGuard admin tunnel.
#
# Usage:
#   sudo infra/host/wg-add-peer.sh <name> [client-public-key]
#
# With a public key (generated on the device with `wg genkey | tee key | wg pubkey`),
# the private key never leaves the device. Without one, a key pair is
# generated here and the full client config is printed once (and as a QR
# code for phones); it is not stored on the server.
#
# Remove a device: delete /etc/wireguard/chat-uses-peers/<name>.conf and run
# `sudo infra/host/bootstrap.sh wireguard`.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $EUID -eq 0 ]] || die "run as root"
[[ $# -ge 1 && $# -le 2 ]] || die "usage: $0 <name> [client-public-key]"
name=$1
client_pub=${2:-}
[[ $name =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || die "name must be lowercase letters, digits and dashes"

load_config
[[ -s $WG_DIR/chat-uses-server.key ]] || die "no server key: run 'bootstrap.sh wireguard' first"
peer_file="$WG_PEERS_DIR/$name.conf"
[[ ! -e $peer_file ]] || die "peer '$name' already exists ($peer_file)"

# Next free address in the tunnel subnet (.2 upwards; .1 is the server).
net_prefix=${WG_HOST_IP%.*}
used=$(grep -h '^AllowedIPs' "$WG_PEERS_DIR"/*.conf 2>/dev/null | grep -oE '[0-9.]+/32' | cut -d/ -f1 || true)
peer_ip=""
for i in $(seq 2 254); do
	candidate="$net_prefix.$i"
	[[ $candidate == "$WG_HOST_IP" ]] && continue
	grep -qx "$candidate" <<<"$used" || { peer_ip=$candidate; break; }
done
[[ -n $peer_ip ]] || die "no free address left in $WG_SUBNET"

client_priv=""
if [[ -z $client_pub ]]; then
	client_priv=$(wg genkey)
	client_pub=$(wg pubkey <<<"$client_priv")
fi
[[ $client_pub =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || die "that does not look like a WireGuard public key"

umask 077
cat >"$peer_file" <<EOF
# $name (added $(date -u +%Y-%m-%dT%H:%MZ))
[Peer]
PublicKey = $client_pub
AllowedIPs = $peer_ip/32
EOF
wg_render_config

endpoint=${WG_ENDPOINT:-<server-public-ip>}
client_conf=$(cat <<EOF
[Interface]
PrivateKey = ${client_priv:-<private key of this device>}
Address = $peer_ip/32

[Peer]
PublicKey = $(wg_server_pubkey)
Endpoint = $endpoint:$WG_PORT
AllowedIPs = $WG_SUBNET, $INFRANET_SUBNET, $CHATNET_SUBNET
PersistentKeepalive = 25
EOF
)

log "Added peer '$name' at $peer_ip"
printf '\n%s\n\n' "$client_conf"
if [[ -n $client_priv ]] && command -v qrencode >/dev/null; then
	qrencode -t ansiutf8 <<<"$client_conf"
fi
[[ -n $client_priv ]] && warn "this private key is shown once and not stored on the server"
info "the tunnel only routes the admin subnets; your normal traffic is unaffected"
