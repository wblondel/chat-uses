#!/usr/bin/env bash
# chat-uses host bootstrap (PLAN.md, Phase 1).
#
# Turns a fresh Debian 13 server into the chat-uses host: SSH hardening,
# automatic security updates, kernel settings, firewall, WireGuard admin
# tunnel, Incus with its storage pool, networks, projects and profiles, and
# brain's restricted access to the Incus API, and the public site (Caddy).
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

STEPS=(packages admin-user ssh system updates firewall wireguard incus storage quotas networks projects profiles incus-api site monitor summary)

PACKAGES=(
	ca-certificates curl jq gettext-base sudo python3 openssl
	unattended-upgrades apt-listchanges
	nftables wireguard-tools qrencode tcpdump
	# dhcp_release: the isolation test frees a stopped container's lease.
	dnsmasq-utils
	# Container-only Incus: the "incus" package adds QEMU for VMs, which this
	# server can't run. Bridges need dnsmasq; AppArmor confines containers.
	# Debian's incus-base only recommends both, and we skip recommends.
	incus-base dnsmasq-base apparmor btrfs-progs
	stress-ng
)

# Zabbly's signing key, pinned in infra/host/zabbly.asc. Published fingerprint:
# https://github.com/zabbly/incus#repository-key (CI checks the file matches).
ZABBLY_FINGERPRINT="4EFC590696CB15B87C73A3AD82CC8797C838DCFD"
ZABBLY_KEYRING=/etc/apt/keyrings/zabbly.asc

# --- steps -------------------------------------------------------------------

step_preflight() {
	[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"

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
	case $INCUS_SOURCE in
	zabbly-stable | zabbly-lts-7.0 | debian) ;;
	*) die "INCUS_SOURCE must be zabbly-stable, zabbly-lts-7.0 or debian" ;;
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
	[[ $TARGET_CPU_ALLOWANCE =~ ^([0-9]+ms/[0-9]+ms|[0-9]+%)$ ]] ||
		die "TARGET_CPU_ALLOWANCE='$TARGET_CPU_ALLOWANCE' must look like 150ms/100ms (hard cap) or 50% (soft share)"
	info "WAN interface: $WAN_IF, public IPv4: ${WG_ENDPOINT:-none found}, CPUs: $ncpu"
}

# Zabbly channel for INCUS_SOURCE, empty for Debian's own package.
zabbly_channel() {
	case $INCUS_SOURCE in
	zabbly-stable) echo stable ;;
	zabbly-lts-7.0) echo lts-7.0 ;;
	esac
}

configure_incus_source() {
	local channel sources file
	channel=$(zabbly_channel)
	sources=/etc/apt/sources.list.d/zabbly-incus-$channel.sources
	# One Zabbly channel at a time.
	for file in /etc/apt/sources.list.d/zabbly-incus-*.sources; do
		[[ -e $file && $file != "$sources" ]] || continue
		rm -f "$file"
		info "removed $file"
	done
	if [[ -z $channel ]]; then
		rm -f "$ZABBLY_KEYRING"
		if dpkg-query -W -f='${Version}' incus-base 2>/dev/null | grep -q '^1:'; then
			warn "Zabbly's Incus is still installed; going back to Debian's needs a manual downgrade (apt-get install --allow-downgrades incus-base=<version> incus-client=<version>)"
		fi
		info "Incus source: Debian"
		return
	fi
	# Zabbly's packages carry an epoch ("1:"), so they outrank Debian's.
	install -D -m 0644 "$CHAT_USES_HOST_DIR/zabbly.asc" "$ZABBLY_KEYRING"
	write_file "$sources" 0644 <<EOF || true
# Managed by chat-uses bootstrap (INCUS_SOURCE=$INCUS_SOURCE).
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/$channel
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: $ZABBLY_KEYRING
EOF
	info "Incus source: Zabbly $channel (key $ZABBLY_FINGERPRINT)"
}

step_packages() {
	log "Installing packages"
	export DEBIAN_FRONTEND=noninteractive
	configure_incus_source
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
	# Not in Debian's "sudo" group: its rule asks for a password, which this
	# user doesn't have, and makes `sudo -v` prompt although commands don't.
	if id -nG "$ADMIN_USER" | grep -qw sudo; then
		gpasswd -d "$ADMIN_USER" sudo >/dev/null
		info "removed $ADMIN_USER from the sudo group (the rule above covers it)"
	fi
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

	local conf=/etc/ssh/sshd_config.d/10-chat-uses.conf backup="" content
	# Render first: a failing render must stop here, not feed an empty file on.
	content=$(render "$CHAT_USES_HOST_DIR/sshd.conf.tmpl")
	[[ -f $conf ]] && backup=$(mktemp) && cp "$conf" "$backup"
	if write_file "$conf" 0644 <<<"$content"; then
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
	# Debian's default only takes Debian security updates; also take Incus
	# from the Zabbly channel in use.
	local channel zabbly_conf=/etc/apt/apt.conf.d/53chat-uses-zabbly
	channel=$(zabbly_channel)
	if [[ -n $channel ]]; then
		write_file "$zabbly_conf" 0644 <<EOF || true
// Managed by chat-uses bootstrap (INCUS_SOURCE=$INCUS_SOURCE). New Incus
// releases install unattended: re-run infra/tests/net-isolation.sh after one.
Unattended-Upgrade::Origins-Pattern {
	"origin=pkgs.zabbly.com,label=incus-$channel";
};
EOF
	else
		rm -f "$zabbly_conf"
	fi
	if have_systemd; then
		systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
	fi
	info "security updates: daily${channel:+, plus Incus from Zabbly $channel}; reboots: manual (check /var/run/reboot-required)"
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
	# stderr hidden: on first use the client suggests "incus admin init",
	# which this setup deliberately replaces with the steps below.
	incus admin waitready --timeout 120 2>/dev/null ||
		die "Incus daemon did not become ready (check: journalctl -u incus)"
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

# Disk limits (PLAN.md D51): Incus enforces each instance's root size with
# btrfs qgroups, whose counts the kernel lets go stale when it deletes a deep
# subvolume. chat-uses-btrfs-quota keeps them right, at boot and every 6 h.
step_quotas() {
	log "Disk limits (btrfs quota accounting)"
	if [[ $POOL_DRIVER != btrfs ]]; then
		info "pool driver $POOL_DRIVER: nothing to keep"
		return
	fi
	local changed=0 pool=/var/lib/incus/storage-pools/$POOL_NAME
	write_file /usr/local/sbin/chat-uses-btrfs-quota 0755 <"$CHAT_USES_HOST_DIR/btrfs-quota.sh" && changed=1
	write_file /etc/systemd/system/chat-uses-btrfs-quota.service 0644 <<UNIT && changed=1
[Unit]
Description=chat-uses: keep btrfs quota counts right on Incus's pool (disk limits)
After=incus.service
Wants=incus.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/chat-uses-btrfs-quota $pool

[Install]
WantedBy=multi-user.target
UNIT
	write_file /etc/systemd/system/chat-uses-btrfs-quota.timer 0644 <<'UNIT' && changed=1
[Unit]
Description=chat-uses: check btrfs quota counts every 6 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
UNIT
	if have_systemd; then
		((changed)) && systemctl daemon-reload
		systemctl enable chat-uses-btrfs-quota.service chat-uses-btrfs-quota.timer >/dev/null 2>&1
		systemctl start chat-uses-btrfs-quota.timer
	fi
	/usr/local/sbin/chat-uses-btrfs-quota "$pool" | sed 's/^/    /' ||
		warn "disk limits: btrfs quota counts are broken (see above)"
}

# Templates are rendered into a variable before "incus ... edit": in a pipe,
# a failing render would still hand Incus an empty config to apply.

apply_network() {
	local name=$1 gw=$2 prefix=$3 file=$4 yaml
	yaml=$(render "$file")
	if ! incus network show "$name" >/dev/null 2>&1; then
		incus network create "$name" --type=bridge "ipv4.address=$gw/$prefix" ipv6.address=none >/dev/null
		info "created $name"
	fi
	incus network edit "$name" <<<"$yaml"
	info "$name: $gw/$prefix"
}

step_networks() {
	log "Networks"
	apply_network "$INFRANET_NAME" "$INFRANET_GW" "$INFRANET_PREFIX" "$CHAT_USES_INCUS_DIR/networks/infranet.yaml"
	apply_network "$CHATNET_NAME" "$CHATNET_GW" "$CHATNET_PREFIX" "$CHAT_USES_INCUS_DIR/networks/chatnet.yaml"
}

step_projects() {
	log "Projects"
	local file name yaml
	for file in "$CHAT_USES_INCUS_DIR"/projects/*.yaml; do
		name=$(basename "$file" .yaml)
		yaml=$(render "$file")
		incus project show "$name" >/dev/null 2>&1 || incus project create "$name" >/dev/null
		incus project edit "$name" <<<"$yaml"
		info "$name"
	done
}

step_profiles() {
	log "Profiles"
	local dir project file name yaml
	for dir in "$CHAT_USES_INCUS_DIR"/profiles/*/; do
		project=$(basename "$dir")
		incus project show "$project" >/dev/null 2>&1 || die "project '$project' missing (run the projects step)"
		for file in "$dir"*.yaml; do
			name=$(basename "$file" .yaml)
			yaml=$(render "$file")
			incus profile show "$name" --project "$project" >/dev/null 2>&1 ||
				incus profile create "$name" --project "$project" >/dev/null
			incus profile edit "$name" --project "$project" <<<"$yaml"
			info "$project/$name"
		done
	done
}

# brain's access to the Incus API (PLAN.md 5.3, D4, D44): a client certificate
# restricted to the "targets" project, over the API listening on infranet's
# address only (the firewall lets only brain in). Panic takes its forensic
# snapshot this way; Phase 6's rewinds will too. images/brain/deploy.sh
# pushes the files into brain.
step_incus_api() {
	log "Incus API for brain (project targets only)"
	local secrets=$CHAT_USES_ETC/secrets address="$INFRANET_GW:$INCUS_API_PORT"
	local crt=$secrets/brain-incus.crt key=$secrets/brain-incus.key fingerprint trusted
	install -d -m 0700 "$secrets"
	if [[ $(incus config get core.https_address) != "$address" ]]; then
		incus config set core.https_address="$address"
		info "API listening on $address (infranet only)"
	else
		info "API already on $address"
	fi
	if [[ ! -s $crt || ! -s $key ]]; then
		(
			umask 077
			openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes -days 3650 \
				-subj /CN=chat-brain -keyout "$key" -out "$crt" 2>/dev/null
		)
		info "made brain's client certificate ($crt)"
	fi
	fingerprint=$(openssl x509 -in "$crt" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f')
	# What Incus trusts as "chat-brain": exactly this certificate, restricted to targets.
	trusted=$(incus query /1.0/certificates?recursion=1 |
		jq -r --arg fp "$fingerprint" '.[] | select(.name == "chat-brain" or .fingerprint == $fp)
			| [.fingerprint, (.restricted | tostring), (.projects // [] | sort | join(","))] | join(" ")')
	if [[ $trusted == "$fingerprint true targets" ]]; then
		info "brain's certificate is trusted, for project targets only"
	else
		while read -r fp _; do
			[[ -n $fp ]] && incus config trust remove "$fp"
		done <<<"$trusted"
		incus config trust add-certificate "$crt" --name chat-brain --restricted --projects targets
		info "brain's certificate trusted, restricted to project targets"
	fi
	install -m 0644 /var/lib/incus/server.crt "$secrets/incus-server.crt"
	info "host certificate copied for brain to pin ($secrets/incus-server.crt)"
}

# The public site (Phase 8, D65): Caddy serves /var/www/chat-uses, which a
# timer keeps a copy of the site brain renders (infra/host/site_sync.py).
step_site() {
	log "Public site (Caddy)"
	if [[ -z $SITE_DOMAIN ]]; then
		info "off: SITE_DOMAIN is empty in $CHAT_USES_CONFIG"
		if have_systemd && systemctl is-enabled --quiet chat-uses-site-sync.timer 2>/dev/null; then
			systemctl disable --now chat-uses-site-sync.timer >/dev/null 2>&1 || true
			info "stopped copying the site from brain (Caddy still runs: systemctl disable --now caddy)"
		fi
		return
	fi
	[[ $SITE_DOMAIN =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] ||
		die "SITE_DOMAIN '$SITE_DOMAIN' isn't a domain name (like chat.example.org)"
	[[ $PUBLIC_HTTPS == 1 ]] ||
		warn "PUBLIC_HTTPS=0: ports 80 and 443 are closed, so nobody (Let's Encrypt included) reaches the site. Set PUBLIC_HTTPS=1, then: bootstrap.sh firewall site"
	if ! command -v caddy >/dev/null; then
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends caddy >/dev/null
		info "installed caddy"
	fi
	install -d -m 0755 /var/www/chat-uses /var/lib/chat-uses
	local changed=0 restart=0 rendered
	rendered=$(mktemp)
	render "$CHAT_USES_HOST_DIR/Caddyfile.tmpl" >"$rendered"
	caddy validate --adapter caddyfile --config "$rendered" >/dev/null 2>&1 ||
		{ rm -f "$rendered"; die "the rendered Caddyfile doesn't validate: caddy validate --config <(...) on infra/host/Caddyfile.tmpl"; }
	write_file /etc/caddy/Caddyfile 0644 <"$rendered" && restart=1
	rm -f "$rendered"
	# Debian's caddy.service runs it as the caddy user; this takes away the
	# rest of the system it doesn't need.
	write_file /etc/systemd/system/caddy.service.d/chat-uses.conf 0644 <<'UNIT' && changed=1 restart=1
[Service]
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/caddy
ProtectHome=yes
PrivateDevices=yes
UNIT
	write_file /usr/local/sbin/chat-uses-site-sync 0755 <"$CHAT_USES_HOST_DIR/site_sync.py" && changed=1
	write_file /etc/systemd/system/chat-uses-site-sync.service 0644 <<'UNIT' && changed=1
[Unit]
Description=chat-uses: copy the public site brain renders, for Caddy
After=incus.service
Wants=incus.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/chat-uses-site-sync
TimeoutStartSec=10min
Nice=10
UNIT
	write_file /etc/systemd/system/chat-uses-site-sync.timer 0644 <<'UNIT' && changed=1
[Unit]
Description=chat-uses: copy the public site from brain every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT
	if have_systemd; then
		((changed)) && systemctl daemon-reload
		systemctl enable --quiet caddy chat-uses-site-sync.timer
		if ((restart)) || ! systemctl is-active --quiet caddy; then
			systemctl restart caddy
		fi
		systemctl start chat-uses-site-sync.timer
		systemctl is-active --quiet caddy || warn "caddy isn't running: journalctl -u caddy"
	fi
	if /usr/local/sbin/chat-uses-site-sync; then
		info "site: $(/usr/local/sbin/chat-uses-site-sync --check 2>&1 | sed 's/^site: //')"
	else
		warn "the site isn't copied yet (brain must run Phase 8's image): journalctl -u chat-uses-site-sync"
	fi
	info "https://$SITE_DOMAIN/ (the certificate comes once DNS points here and 80/443 are open)"
}

# The host's monitor (Phase 10): every minute, it tells #ops, through a
# Discord webhook, when brain, chat, a container, the encoder, the disk or
# the CPU goes wrong, and when it's back to normal (infra/host/monitor.py).
step_monitor() {
	log "Monitor (alerts in #ops)"
	install -d -m 0755 /var/lib/chat-uses
	[[ -d $CHAT_USES_ETC/secrets ]] || install -d -m 0700 "$CHAT_USES_ETC/secrets"
	local changed=0
	write_file /usr/local/sbin/chat-uses-monitor 0755 <"$CHAT_USES_HOST_DIR/monitor.py" && changed=1
	# What it needs from host.env: the installed copy has no lib.sh to read it.
	printf 'BRAIN_IP=%s\nBRAIN_PORT=%s\nPOOL_NAME=%s\n' "$BRAIN_IP" "$BRAIN_PORT" "$POOL_NAME" |
		write_file "$CHAT_USES_ETC/monitor.env" 0644 || true
	write_file /etc/systemd/system/chat-uses-monitor.service 0644 <<'UNIT' && changed=1
[Unit]
Description=chat-uses: check the stream and tell #ops what's wrong
After=incus.service
Wants=incus.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/chat-uses-monitor
TimeoutStartSec=2min
Nice=5
UNIT
	write_file /etc/systemd/system/chat-uses-monitor.timer 0644 <<'UNIT' && changed=1
[Unit]
Description=chat-uses: check the stream every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT
	if have_systemd; then
		((changed)) && systemctl daemon-reload
		systemctl enable --quiet chat-uses-monitor.timer
		systemctl start chat-uses-monitor.timer
	fi
	local line
	while IFS= read -r line; do
		info "${line#monitor: }"
	done < <(/usr/local/sbin/chat-uses-monitor --check 2>&1 || true)
	if [[ -s $CHAT_USES_ETC/secrets/ops-webhook ]]; then
		info "test the webhook: sudo chat-uses-monitor --test"
	else
		warn "alerts can't reach #ops yet: in Discord, #ops → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL, then: sudo install -m 0600 /dev/stdin $CHAT_USES_ETC/secrets/ops-webhook (paste it, Enter, Ctrl-D)"
	fi
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
	if [[ $POOL_DRIVER == btrfs ]]; then
		local uuid
		uuid=$(btrfs filesystem show "/var/lib/incus/storage-pools/$POOL_NAME" 2>/dev/null | awk '/uuid:/ {print $NF; exit}' || true)
		case $(cat "/sys/fs/btrfs/$uuid/qgroups/inconsistent" 2>/dev/null || echo off) in
		0) info "disk limits: quota counts consistent" ;;
		1) warn "disk limits: quota counts inconsistent (run: bootstrap.sh quotas)" ;;
		*) warn "disk limits: quotas off on the pool (run: bootstrap.sh quotas)" ;;
		esac
	fi
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
