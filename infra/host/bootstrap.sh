#!/usr/bin/env bash
# chat-uses host bootstrap (PLAN.md, Phase 1).
#
# Turns a fresh Debian 13 server into the chat-uses host: SSH hardening,
# automatic security updates, kernel settings, firewall, WireGuard admin
# tunnel, Incus with its storage pool, networks, projects and profiles.
#
# Idempotent: run it again after every change to infra/. Each step only
# changes what differs from the repository.
#
# Usage:
#   sudo infra/host/bootstrap.sh            # all steps, in order
#   sudo infra/host/bootstrap.sh firewall   # only some steps
#   infra/host/bootstrap.sh --list
#
# Configuration: infra/host/host.env.example, overridden by
# /etc/chat-uses/host.env (or the file in $CHAT_USES_CONFIG).

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STEPS=(packages admin-user ssh system updates firewall wireguard incus storage networks projects profiles summary)

PACKAGES=(
	ca-certificates curl jq gettext-base sudo
	unattended-upgrades apt-listchanges
	nftables wireguard-tools qrencode tcpdump
	incus btrfs-progs
	stress-ng
)

# --- steps -------------------------------------------------------------------

step_preflight() {
	[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
	command -v envsubst >/dev/null || die "envsubst missing: apt-get install gettext-base"

	if [[ -r /etc/os-release ]]; then
		# shellcheck disable=SC1091
		. /etc/os-release
		if [[ ${ID:-} != debian || ${VERSION_ID:-} != 13 ]]; then
			[[ ${ALLOW_UNSUPPORTED_OS:-0} == 1 ]] ||
				die "expected Debian 13, found ${PRETTY_NAME:-unknown} (set ALLOW_UNSUPPORTED_OS=1 to continue anyway)"
			warn "unsupported OS ${PRETTY_NAME:-unknown}; continuing because ALLOW_UNSUPPORTED_OS=1"
		fi
	fi

	load_config
	if [[ -f $CHAT_USES_CONFIG ]]; then
		info "config: $CHAT_USES_CONFIG"
	else
		info "config: defaults (no $CHAT_USES_CONFIG)"
	fi

	if [[ -z $WAN_IF ]] || ! ip link show "$WAN_IF" >/dev/null 2>&1; then
		die "public interface '${WAN_IF}' not found; set WAN_IF in $CHAT_USES_CONFIG"
	fi
	case $SSH_PERMIT_ROOT_LOGIN in
	prohibit-password | no) ;;
	*) die "SSH_PERMIT_ROOT_LOGIN must be 'prohibit-password' or 'no'" ;;
	esac
	case $POOL_DRIVER in
	btrfs | dir | zfs | lvm) ;;
	*) die "unsupported POOL_DRIVER '$POOL_DRIVER'" ;;
	esac

	# CPU pinning must reference CPUs that exist, written as ranges/lists.
	local ncpu var spec max
	ncpu=$(nproc --all)
	for var in CPUS_BCAST CPUS_BRAIN CPUS_SHARED CPUS_TARGETS; do
		spec=${!var}
		[[ $spec =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ && $spec =~ [-,] ]] ||
			die "$var='$spec' must be a CPU range or list (for one CPU write '2-2')"
		max=$(tr ',-' '\n' <<<"$spec" | sort -n | tail -1)
		((max < ncpu)) || die "$var='$spec' uses CPU $max but this server has $ncpu CPUs (0-$((ncpu - 1)))"
	done
	info "WAN interface: $WAN_IF, public IPv4: ${WG_ENDPOINT:-none found}, CPUs: $ncpu"
}

step_packages() {
	log "Installing packages"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}" >/dev/null
	info "installed: ${PACKAGES[*]}"
}

step_admin_user() {
	log "Admin user"
	if [[ -z $ADMIN_USER ]]; then
		info "ADMIN_USER empty: skipped"
		return
	fi
	if ! id "$ADMIN_USER" >/dev/null 2>&1; then
		useradd --create-home --shell /bin/bash "$ADMIN_USER"
		info "created user $ADMIN_USER"
	fi
	usermod -aG sudo "$ADMIN_USER"
	getent group incus-admin >/dev/null && usermod -aG incus-admin "$ADMIN_USER"

	local home keys
	home=$(getent passwd "$ADMIN_USER" | cut -d: -f6)
	keys="$home/.ssh/authorized_keys"
	if [[ ! -s $keys && -s /root/.ssh/authorized_keys ]]; then
		install -d -m 0700 -o "$ADMIN_USER" -g "$ADMIN_USER" "$home/.ssh"
		install -m 0600 -o "$ADMIN_USER" -g "$ADMIN_USER" /root/.ssh/authorized_keys "$keys"
		info "copied root's SSH keys to $keys"
	fi

	# Key-only login means no password to type, so sudo must not ask for one.
	local sudoers=/etc/sudoers.d/chat-uses-admin tmp
	tmp=$(mktemp)
	printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$ADMIN_USER" >"$tmp"
	visudo -cf "$tmp" >/dev/null || die "generated sudoers file is invalid"
	if write_file "$sudoers" 0440 <"$tmp"; then
		info "passwordless sudo for $ADMIN_USER"
	fi
	rm -f "$tmp"
}

step_ssh() {
	log "SSH hardening"
	command -v sshd >/dev/null || die "openssh-server is not installed"

	# Refuse to disable passwords unless someone can still log in with a key.
	local root_keys=0 admin_keys=0 home
	[[ -s /root/.ssh/authorized_keys ]] && root_keys=1
	if [[ -n $ADMIN_USER ]] && id "$ADMIN_USER" >/dev/null 2>&1; then
		home=$(getent passwd "$ADMIN_USER" | cut -d: -f6)
		[[ -s $home/.ssh/authorized_keys ]] && admin_keys=1
	fi
	if [[ $SSH_PERMIT_ROOT_LOGIN == no ]]; then
		((admin_keys)) || die "SSH_PERMIT_ROOT_LOGIN=no but ADMIN_USER has no authorized_keys: you would be locked out"
	else
		((root_keys || admin_keys)) || die "no authorized_keys for root or ADMIN_USER: you would be locked out"
	fi

	if [[ $SSH_PORT != 22 ]] && have_systemd && systemctl is-active --quiet ssh.socket; then
		die "sshd is socket-activated (ssh.socket); change the port in ssh.socket instead of SSH_PORT"
	fi

	local conf=/etc/ssh/sshd_config.d/10-chat-uses.conf backup=""
	[[ -f $conf ]] && backup=$(mktemp) && cp "$conf" "$backup"
	if render "$CHAT_USES_HOST_DIR/sshd.conf.tmpl" | write_file "$conf" 0644; then
		install -d -m 0755 /run/sshd # sshd -t needs it; normally created by ssh.service
		if ! sshd -t; then
			if [[ -n $backup ]]; then cp "$backup" "$conf"; else rm -f "$conf"; fi
			die "sshd rejected the new config; restored the previous one"
		fi
		if have_systemd; then
			systemctl reload ssh
			info "sshd reloaded (keys only, port $SSH_PORT, root: $SSH_PERMIT_ROOT_LOGIN)"
			warn "check that a NEW ssh session works before closing this one"
		else
			warn "no systemd: reload sshd yourself"
		fi
	else
		info "already up to date"
	fi
	[[ -n $backup ]] && rm -f "$backup"
	return 0
}

step_system() {
	log "Timezone and kernel settings"
	if [[ -f /usr/share/zoneinfo/$TIMEZONE ]]; then
		ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
		printf '%s\n' "$TIMEZONE" >/etc/timezone
		info "timezone: $TIMEZONE"
	else
		warn "unknown timezone '$TIMEZONE': left unchanged"
	fi
	# Restricted Incus projects can't set limits.memory.swap, so a target
	# could push the host into swap and make the stream stutter.
	if [[ -n $(swapon --noheadings --show 2>/dev/null) ]]; then
		warn "swap is enabled; targets can use it. Consider 'swapoff -a' and removing it from /etc/fstab"
	fi
	if write_file /etc/sysctl.d/60-chat-uses.conf 0644 <"$CHAT_USES_HOST_DIR/sysctl.conf"; then
		sysctl -q -e -p /etc/sysctl.d/60-chat-uses.conf
		info "sysctl settings applied"
	else
		info "sysctl settings already up to date"
	fi
}

step_updates() {
	log "Automatic security updates"
	write_file /etc/apt/apt.conf.d/20auto-upgrades 0644 <<'EOF' || true
// Managed by chat-uses bootstrap.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
	write_file /etc/apt/apt.conf.d/52chat-uses-unattended 0644 <<'EOF' || true
// Managed by chat-uses bootstrap. Security updates install automatically;
// reboots stay manual so they happen in an announced window (PLAN.md 10.3).
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
	if have_systemd; then
		systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
	fi
	info "security updates: daily; reboots: manual (check /var/run/reboot-required)"
}

step_firewall() {
	log "Host firewall (nftables)"
	local rendered=$CHAT_USES_ETC/nftables.nft

	install -d -m 0755 "$CHAT_USES_ETC"
	render "$CHAT_USES_HOST_DIR/nftables.nft.tmpl" >"$rendered.new"
	nft -c -f "$rendered.new" || die "nftables rejected the rendered ruleset ($rendered.new)"
	mv "$rendered.new" "$rendered"

	# Debian's default /etc/nftables.conf starts with "flush ruleset", which
	# would also delete Incus's tables. Replace it with an include of ours.
	if [[ -f /etc/nftables.conf && ! -f /etc/nftables.conf.chat-uses-orig ]] &&
		! grep -q 'chat-uses' /etc/nftables.conf; then
		cp /etc/nftables.conf /etc/nftables.conf.chat-uses-orig
	fi
	write_file /etc/nftables.conf 0755 <<EOF || true
#!/usr/sbin/nft -f
# Managed by chat-uses bootstrap. Deliberately no "flush ruleset":
# that would also remove the tables Incus manages.
include "$rendered"
EOF

	# The stock unit flushes the whole ruleset on stop; only remove ours.
	if have_systemd; then
		if write_file /etc/systemd/system/nftables.service.d/chat-uses.conf 0644 <<'EOF'; then
[Service]
ExecStop=
ExecStop=-/usr/sbin/nft delete table inet chat_uses_host
EOF
			systemctl daemon-reload
		fi
		systemctl enable nftables >/dev/null 2>&1
	fi

	nft -f /etc/nftables.conf
	info "table inet chat_uses_host loaded (ssh $SSH_PORT, wireguard $WG_PORT/udp, https: $PUBLIC_HTTPS)"
}

step_wireguard() {
	log "WireGuard admin tunnel"
	install -d -m 0700 "$WG_DIR" "$WG_PEERS_DIR"
	if [[ ! -s $WG_DIR/chat-uses-server.key ]]; then
		(umask 077 && wg genkey >"$WG_DIR/chat-uses-server.key")
		wg pubkey <"$WG_DIR/chat-uses-server.key" >"$WG_DIR/chat-uses-server.pub"
		info "generated server key"
	fi
	wg_render_config
	info "server public key: $(wg_server_pubkey)"
	info "add a device with: infra/host/wg-add-peer.sh <name>"
}

step_incus() {
	log "Incus daemon"
	command -v incus >/dev/null || die "incus is not installed (run the packages step)"
	if have_systemd; then
		systemctl enable --now incus.socket incus.service >/dev/null 2>&1 || true
	fi
	incus admin waitready --timeout 120 || die "Incus daemon did not become ready"
	info "Incus $(incus version | awk -F': ' '/Server version/ {print $2}') ready"
}

step_storage() {
	log "Storage pool '$POOL_NAME' ($POOL_DRIVER)"
	if incus storage show "$POOL_NAME" >/dev/null 2>&1; then
		local driver
		driver=$(incus storage show "$POOL_NAME" | awk '/^driver:/ {print $2}')
		[[ $driver == "$POOL_DRIVER" ]] || warn "pool '$POOL_NAME' uses driver '$driver', not '$POOL_DRIVER' (not changed)"
		info "already exists"
		return
	fi
	local args=()
	[[ $POOL_DRIVER != dir ]] && args+=("size=$POOL_SIZE")
	incus storage create "$POOL_NAME" "$POOL_DRIVER" "${args[@]}" >/dev/null
	info "created (${args[*]:-no size limit})"
}

apply_network() {
	local name=$1 gw=$2 prefix=$3 file=$4
	if ! incus network show "$name" >/dev/null 2>&1; then
		incus network create "$name" --type=bridge "ipv4.address=$gw/$prefix" ipv6.address=none >/dev/null
		info "created $name"
	fi
	render "$file" | incus network edit "$name"
	info "$name: $gw/$prefix"
}

step_networks() {
	log "Networks"
	apply_network "$INFRANET_NAME" "$INFRANET_GW" "$INFRANET_PREFIX" "$CHAT_USES_INCUS_DIR/networks/infranet.yaml"
	apply_network "$CHATNET_NAME" "$CHATNET_GW" "$CHATNET_PREFIX" "$CHAT_USES_INCUS_DIR/networks/chatnet.yaml"
}

step_projects() {
	log "Projects"
	local file name
	for file in "$CHAT_USES_INCUS_DIR"/projects/*.yaml; do
		name=$(basename "$file" .yaml)
		incus project show "$name" >/dev/null 2>&1 || incus project create "$name" >/dev/null
		render "$file" | incus project edit "$name"
		info "$name"
	done
}

step_profiles() {
	log "Profiles"
	local dir project file name
	for dir in "$CHAT_USES_INCUS_DIR"/profiles/*/; do
		project=$(basename "$dir")
		incus project show "$project" >/dev/null 2>&1 || die "project '$project' missing (run the projects step)"
		for file in "$dir"*.yaml; do
			name=$(basename "$file" .yaml)
			incus profile show "$name" --project "$project" >/dev/null 2>&1 ||
				incus profile create "$name" --project "$project" >/dev/null
			render "$file" | incus profile edit "$name" --project "$project"
			info "$project/$name"
		done
	done
}

step_summary() {
	log "Summary"
	local net project
	for net in "$INFRANET_NAME" "$CHATNET_NAME"; do
		info "network $net: $(incus network get "$net" ipv4.address 2>/dev/null || echo missing)"
	done
	for project in infra targets; do
		if incus project show "$project" >/dev/null 2>&1; then
			info "project $project: profiles $(incus profile list --project "$project" --format csv 2>/dev/null | cut -d, -f1 | grep -vx default | tr '\n' ' ')"
		else
			warn "project $project missing"
		fi
	done
	if nft list table inet chat_uses_host >/dev/null 2>&1; then
		info "firewall: loaded"
	else
		warn "firewall table not loaded"
	fi
	if ip link show "$WG_IF" >/dev/null 2>&1; then
		info "wireguard: $WG_IF up, $(find "$WG_PEERS_DIR" -name '*.conf' 2>/dev/null | wc -l) peer(s)"
	else
		info "wireguard: $WG_IF down"
	fi
	cat <<EOF

Next:
  1. Open a NEW ssh session to check you can still log in.
  2. Add your laptop to the tunnel:  infra/host/wg-add-peer.sh laptop
  3. Check isolation:                infra/tests/net-isolation.sh
EOF
}

# --- main --------------------------------------------------------------------

usage() {
	sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
	printf 'Steps: %s\n' "${STEPS[*]}"
}

main() {
	local requested=() step known
	for arg in "$@"; do
		case $arg in
		-h | --help) usage; exit 0 ;;
		--list) printf '%s\n' "${STEPS[@]}"; exit 0 ;;
		-*) die "unknown option $arg" ;;
		*)
			known=0
			for step in "${STEPS[@]}"; do [[ $step == "$arg" ]] && known=1; done
			((known)) || die "unknown step '$arg' (see --list)"
			requested+=("$arg")
			;;
		esac
	done
	((${#requested[@]})) || requested=("${STEPS[@]}")

	step_preflight
	for step in "${requested[@]}"; do
		"step_${step//-/_}"
	done
}

main "$@"
