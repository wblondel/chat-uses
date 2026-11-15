# Phase 1 — Host foundation (runbook)

Turns a fresh Debian 13 server into the chat-uses host with one idempotent
script, then proves that a chat-controlled container can't get out.

**Done when** (from PLAN.md): the isolation test passes on the server, still
passes after a reboot, and `bootstrap.sh` can rebuild the setup from scratch.

## What you get

| Piece | Details |
|---|---|
| SSH | Keys only, root by key (`prohibit-password`), optional admin user with sudo. Refuses to apply if nobody has a key. |
| Updates | Daily automatic security updates; reboots stay manual. |
| Kernel | `dmesg`/kernel pointers hidden from containers, routing on, Incus-recommended limits. |
| Firewall | nftables table `inet chat_uses_host`: default drop; public SSH and WireGuard only; the network rules below. Coexists with Incus's own tables. |
| WireGuard | `wg0` admin tunnel (`10.99.0.0/24`) to reach infrastructure and view targets. |
| Incus | btrfs pool `chat`; networks `infranet` and `chatnet`; projects `infra` and `targets` (restricted); profiles `bcast`, `brain`, `egress`, `intranet`, `target-desktop`. |

### Network model

```
internet ── WAN ──┐
                  │  host firewall (nftables), default drop
 wg0 (admin) ─────┤
                  ├── infranet 10.20.0.0/24  (NAT to internet)
                  │     bcast .10 · brain .11 · egress .12 · intranet .13
                  └── chatnet  10.30.0.0/24  (no NAT, no IPv6, no DNS)
                        targets
```

Traffic between the two networks is **routed through the host**, so every
packet passes the host firewall:

| From | To | Allowed |
|---|---|---|
| chatnet | egress `:3128`, intranet `:80` | yes: the only ways out |
| chatnet | anything else (internet, host, other infra ports, DNS) | **dropped** |
| brain | targets `:5900` (VNC), `:4444` (QMP) | yes |
| bcast | targets `:5900` (VNC), `:4712` (audio) | yes |
| infranet | internet | yes (NAT) |
| wg0 | infranet: all; targets: `:5900`, `:4712` | yes |
| internet | host `:22` (rate-limited), `:51820/udp` | yes; `80/443` when `PUBLIC_HTTPS=1` |

Targets also get `security.port_isolation` (they can't talk to each other),
MAC/IPv4 anti-spoofing, and bandwidth caps.

## Before you start

- [ ] Debian 13 installed from the netcup image, your SSH key in `/root/.ssh/authorized_keys`.
- [ ] You know root's password for the netcup **web console** (recovery path).
- [ ] Phase 0 server check done (or run `infra/host/server-check.sh` now).

## Steps

### 1. Repository and config

```bash
apt-get update && apt-get install -y git
git clone https://github.com/wblondel/chat-uses /opt/chat-uses   # skip if done in Phase 0
cd /opt/chat-uses
install -D -m 0600 infra/host/host.env.example /etc/chat-uses/host.env
nano /etc/chat-uses/host.env
```

Review at least:

- `CPUS_*`: defaults assume 8 vCPUs (`0-1` broadcast, `2-2` brain + egress + intranet, `3-7` targets). The script refuses CPU numbers that don't exist.
- `ADMIN_USER`: optional. Set it if you want a non-root login with sudo.
- `SSH_PORT`, `TIMEZONE`.

Everything else can stay at its default.

### 2. Run the bootstrap

Keep your current SSH session open the whole time.

```bash
sudo infra/host/bootstrap.sh
```

It runs these steps in order (you can run any subset, e.g. `bootstrap.sh firewall`):

| Step | Does |
|---|---|
| `packages` | Adds the Zabbly Incus repository (`INCUS_SOURCE`, key pinned in `infra/host/zabbly.asc`), then installs container-only Incus (`incus-base`, `dnsmasq-base` for bridges, AppArmor), nftables, WireGuard tools, btrfs-progs, tcpdump, stress-ng, unattended-upgrades… |
| `admin-user` | Creates `ADMIN_USER` (if set), copies root's keys, passwordless sudo, `incus-admin` group |
| `ssh` | Writes `/etc/ssh/sshd_config.d/10-chat-uses.conf`, validates with `sshd -t`, rolls back on error, reloads |
| `system` | Timezone, `/etc/sysctl.d/60-chat-uses.conf`, swap warning |
| `updates` | `/etc/apt/apt.conf.d/20auto-upgrades` and `52chat-uses-unattended` |
| `firewall` | Renders `/etc/chat-uses/nftables.nft`, checks it with `nft -c`, makes `/etc/nftables.conf` include it (Debian's default would `flush ruleset` and wipe Incus's rules), loads it |
| `wireguard` | Server key, `/etc/wireguard/wg0.conf` from the peer files, starts `wg-quick@wg0` |
| `incus` | Starts the daemon and waits for it |
| `storage` | Creates the pool (`btrfs`, loop-backed, `POOL_SIZE`) if missing |
| `networks` | Creates/updates `infranet` and `chatnet` from `infra/incus/networks/` |
| `projects` | Creates/updates `infra` and `targets` from `infra/incus/projects/` |
| `profiles` | Creates/updates profiles from `infra/incus/profiles/<project>/` |
| `summary` | Prints the state and next steps |

### 3. Check SSH still works

From your laptop, in a **new** terminal: `ssh root@SERVER` (or your admin user).
Only close the old session once this works.

### 4. Connect your laptop to the admin tunnel

Install WireGuard on the laptop. Best: generate the key on the laptop so it never
leaves it.

```bash
# laptop
wg genkey | tee laptop.key | wg pubkey        # prints the public key
# server
sudo infra/host/wg-add-peer.sh laptop <LAPTOP_PUBLIC_KEY>
```

Put the printed config in your WireGuard app, replacing the `PrivateKey`
placeholder with the contents of `laptop.key`. (For a phone, run
`wg-add-peer.sh phone` without a key: it prints a complete config and a QR code
once, and doesn't keep the private key.)

Test: `ping 10.99.0.1` from the laptop. Only the admin subnets go through the
tunnel; the rest of your traffic is unaffected.

Remove a device: delete `/etc/wireguard/chat-uses-peers/<name>.conf`, then
`sudo infra/host/bootstrap.sh wireguard`.

### 5. Run the isolation test

```bash
sudo infra/tests/net-isolation.sh
```

About two minutes. It launches three throwaway containers (a target on chatnet
and fake egress/intranet services listening on several ports), then checks:

- the target reaches the proxy and intranet ports;
- the target can't reach the internet, outside DNS, SMTP, the host's SSH/DNS/API
  ports, or any other port on the infra containers (each one really listening,
  so a pass proves the firewall dropped it);
- no packet with a chatnet source address leaves on the public interface (tcpdump),
  which matters because a no-NAT network can leak packets that never get replies;
- no global IPv6, no name resolution.

All lines should say PASS. `--keep` leaves the containers for poking around.

### 6. Reboot and re-test

```bash
sudo reboot
# after it's back
sudo nft list tables                 # expect: inet chat_uses_host, inet incus (and bridge incus)
sudo infra/tests/net-isolation.sh
```

- [ ] All PASS after reboot → tick Phase 1 in PLAN.md.

## Changing things later

- Edit `/etc/chat-uses/host.env` or the files under `infra/`, `git pull`, then re-run
  `bootstrap.sh` (all steps, or just the ones you touched).
- Networks, projects and profiles are **replaced** from the YAML files on every run.
  Don't change them by hand with `incus … edit`; put the change in the repository.
- `systemctl restart nftables` is safe: the unit is overridden to only remove
  the chat-uses table on stop, not Incus's.

### Incus comes from Zabbly

By default (`INCUS_SOURCE="zabbly-stable"`) Incus is the latest release from
[Zabbly's repository](https://github.com/zabbly/incus), built and signed by the
Incus project lead. Debian 13's own Incus can't restore snapshots in the
restricted `targets` project, and rewind depends on that (PLAN.md D19, D20).

- **Updates install automatically**: `unattended-upgrades` also takes the Zabbly
  channel (`/etc/apt/apt.conf.d/53chat-uses-zabbly`). A new Incus release arrives
  about once a month and can change behaviour, so after one
  (`grep incus /var/log/unattended-upgrades/unattended-upgrades.log`), re-run
  `infra/tests/net-isolation.sh`.
- **The signing key is pinned** in `infra/host/zabbly.asc`. CI checks it against
  `ZABBLY_FINGERPRINT` in `bootstrap.sh` (published fingerprint
  `4EFC 5906 96CB 15B8 7C73  A3AD 82CC 8797 C838 DCFD`). The key expires
  **2030-08-17**. When Zabbly publishes a new one, verify it against their
  README, replace the file and the fingerprint, and re-run `bootstrap.sh packages`.
- **Other channels**: `zabbly-lts-7.0` (7.0 LTS; snapshot restore in `targets`
  still fails with 7.0.2) or `debian` (Debian's 6.0 package). Going from Zabbly
  back to Debian needs a manual downgrade, which the script explains.

## Recovery

| Problem | Fix |
|---|---|
| SSH locked out | netcup web console, log in as root with the password, then `rm /etc/ssh/sshd_config.d/10-chat-uses.conf && systemctl reload ssh` |
| Firewall blocks something you need | `nft delete table inet chat_uses_host` turns it off (Incus keeps working); fix the template; `bootstrap.sh firewall` turns it back on |
| See what's dropped | `journalctl -k -g chat-uses` (drops are logged, rate-limited) |
| Swap warning | `swapoff -a` and comment the swap line in `/etc/fstab`. Restricted Incus projects can't stop targets from using host swap. |
| Instances | `incus list --all-projects` |

## What was tested before this landed

Run in a simulated server: a fresh, minimal **Debian 13.7** root filesystem
(debootstrap, so even `sudo` and `envsubst` were missing), inside an isolated
network namespace with a veth as the public interface. Debian packages
(nftables 1.1.3, OpenSSH 10.0, …) plus **Incus 7.6 from Zabbly's stable
channel**, as installed by the `packages` step. An earlier round used Debian's
own Incus 6.0.4; both pass everything below except snapshot restore.

- **One full `bootstrap.sh` run with no step list**, packages included, then a
  second full run that changes nothing.
- `unattended-upgrades --dry-run` lists `origin=pkgs.zabbly.com,label=incus-stable`
  next to Debian's origins.
- Our firewall table and Incus's tables coexist; containers get their addresses.
- Isolation test: **24/24 pass**, with a real `sshd` listening on the host. With the
  firewall removed, **15 checks fail** (internet leak on the public interface,
  host DNS/SSH, every infra decoy port), so the test detects a broken firewall.
- **btrfs pool** created by the `storage` step (loop file); a 1 GiB root quota stops
  `dd` with "Disk quota exceeded" (red-team test 3).
- `ssh` step: effective config is keys-only (`sshd -T`); refuses without a key; rolls
  back an invalid config.
- `admin-user`: user created with root's key, `sudo` and `incus-admin` groups,
  passwordless sudo.
- `wg-add-peer.sh` and `server-check.sh` work with Debian's `mawk`.
- A template with a typo stops its step before anything is applied.
- **Snapshot restore in the restricted `targets` project** (btrfs, container
  running): works in about 1 second on Incus 7.6.
- **CVE-2026-48751 replay**: a snapshot carrying a `raw.lxc` hook, moved into
  `targets`, is refused on restore ("Use of low-level config `raw.lxc` … is
  forbidden").
- shellcheck clean; CI runs on Ubuntu 26.04 and checks the pinned Zabbly key.

Bugs this run found and fixed: the script required `envsubst` before the step
that installs it; a mistyped `WAN_IF` made it exit silently; Debian's `incus`
package pulls in QEMU while `incus-base` skips the `dnsmasq-base` bridges need;
and a failed template could hand Incus an empty config.

**Why Zabbly: snapshot restore.** With Debian's Incus 6.0.4 (and Debian's
backported 7.0.2), `incus snapshot restore` is refused in the `targets` project
("Changing `volatile.*` … is forbidden"). Since the CVE-2026-48751 fix, a restore
is checked against the project's low-level rule, and that check also caught
Incus's own `volatile.*` keys. Upstream fixed it in Incus 7.5 (lxc/incus PR #3994);
Zabbly's stable channel has it. See PLAN.md D19 and D20.

**Not tested there** (this sandbox's kernel or the lack of systemd prevents it),
so watch these on the real server:

- memory, process, CPU-share and disk-priority limits, and NIC bandwidth caps
  (the simulation removed them; Debian 13 with cgroup v2 supports them). CPU
  pinning was accepted (containers started with it) but not measured;
- AppArmor confinement of containers (no AppArmor in the sandbox kernel);
- bringing up the WireGuard interface (no kernel module);
- systemd: services enabled and started by the scripts, `sshd` reload, DHCP inside
  containers (test containers used fixed addresses), and behaviour across a reboot.

The Incus HTTPS API stays off in this phase; Phase 4 turns it on for brain with a
certificate restricted to the `targets` project.
