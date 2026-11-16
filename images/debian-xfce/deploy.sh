#!/usr/bin/env bash
# Deploy the target on stream: the desktop chat controls (PLAN.md 6.7, D59).
#
# Usage:
#   sudo images/debian-xfce/deploy.sh             create the target if missing, else say how it is
#   sudo images/debian-xfce/deploy.sh --replace   recreate it from the latest chat-uses/debian-xfce image:
#                                                 a fresh desktop. Everything chat did on the old one
#                                                 goes, its snapshots and saves too
#   --image ALIAS                                 launch from another image (default chat-uses/debian-xfce)
#
# The target is TARGET_NAME (chatpc), which is also its hostname (chat sees
# it in the terminal), at TARGET_IP (10.30.0.10) in project "targets", with
# the target-desktop profile's limits (MEM_TARGET, DISK_TARGET, CPUs,
# network). bcast and brain show and drive TARGET_IP unless a TARGET_HOST
# line in /etc/chat-uses/bcast.local.env or brain.local.env says otherwise;
# this script says so if one does.
#
# A fresh target gets a snapshot named `golden` once its desktop answers:
# the desktop as deployed. Each new run starts from it (control/src/chatuses/
# game/runs.py): brain restores it when a run dies. Chat can't reach it.
#
# images/debian-xfce/check.sh launches its own throwaway test target
# (phase2-check at .251); check the real one with
# `images/debian-xfce/check.sh --instance chatpc`.

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../infra/host/lib.sh"

IMAGE=chat-uses/debian-xfce
REPLACE=0
while (($#)); do
	case $1 in
	--replace) REPLACE=1 ;;
	--image) IMAGE=${2:?}; shift ;;
	-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
	*) die "unknown argument $1" ;;
	esac
	shift
done
[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is required (bootstrap's packages step installs it)"
load_config

NAME=$TARGET_NAME
[[ $NAME =~ ^[a-z][a-z0-9-]{0,62}$ ]] || die "TARGET_NAME=$NAME: use lowercase letters, digits and dashes (it's a hostname)"
BOOT_WAIT=${BOOT_WAIT:-120} # seconds to wait for the desktop's VNC

# Who holds the target's address, if anyone: "project/name". A stopped
# instance keeps its address (its NIC's ipv4.address), so it's read from the
# instances' devices, not from the addresses they have now.
holder() {
	incus list --all-projects --format json | python3 -c '
import json, sys
for i in json.load(sys.stdin):
    if any(d.get("ipv4.address") == sys.argv[1] for d in (i.get("expanded_devices") or {}).values()):
        print(i["project"] + "/" + i["name"])
        break
' "$TARGET_IP"
}

exists=0
incus info "$NAME" --project targets >/dev/null 2>&1 && exists=1
if ((exists && REPLACE)); then
	snaps=$(incus query "/1.0/instances/$NAME/snapshots?project=targets" | grep -c '"/1.0/' || true)
	log "Removing the old $NAME and its $snaps snapshots"
	incus delete --force "$NAME" --project targets >/dev/null
	exists=0
fi
if ((!exists)); then
	held=$(holder)
	[[ -z $held ]] || die "$TARGET_IP is held by $held: free it (or set TARGET_IP in host.env)"
	log "Launching $NAME from $IMAGE at $TARGET_IP"
	if ! incus launch "$IMAGE" "$NAME" --project targets -p target-desktop --device "eth0,ipv4.address=$TARGET_IP" >/dev/null; then
		incus delete --force "$NAME" --project targets >/dev/null 2>&1 || true # created but not started
		die "couldn't launch $NAME (the error above)"
	fi
	t0=$SECONDS
	up=0
	while ((SECONDS - t0 < BOOT_WAIT)); do
		if timeout 2 bash -c "exec 3<>/dev/tcp/$TARGET_IP/5900" 2>/dev/null; then up=1; break; fi
		sleep 1
	done
	if ((up)); then
		info "the desktop's VNC answers on $TARGET_IP:5900 ($((SECONDS - t0)) s after launch)"
		sleep 10 # let the session settle: the golden snapshot is the desktop each run starts from
		incus snapshot create "$NAME" golden --project targets
		info "took its golden snapshot: each new run starts from it"
	else
		warn "VNC didn't answer on $TARGET_IP:5900 within $BOOT_WAIT s: images/debian-xfce/check.sh --instance $NAME says why"
		warn "no golden snapshot taken: runs will continue on the same desktop (deploy again with --replace)"
	fi
	# brain forgets the snapshots the target no longer has when it starts.
	if ((REPLACE)) && incus exec brain --project infra -- systemctl restart chat-brain 2>/dev/null; then
		info "restarted brain: its timeline forgot the old target's snapshots"
	fi
else
	held=$(holder)
	if [[ $held != "targets/$NAME" ]]; then
		warn "$NAME doesn't hold $TARGET_IP (${held:-nobody} does), where bcast and brain look"
	fi
fi

log "$NAME"
incus list "$NAME" --project targets --format csv -c s4 | sed 's/^/    state, address: /'
if ! incus query "/1.0/instances/$NAME/snapshots?project=targets" | grep -qE '/snapshots/golden("|\?)'; then
	warn "$NAME has no golden snapshot: new runs will continue on the same desktop (deploy.sh --replace gives it one)"
fi
built=$(incus config get "$NAME" image.chat-uses.commit --project targets 2>/dev/null || true)
head=$(git -C "$CHAT_USES_REPO" rev-parse --short HEAD 2>/dev/null || true)
if [[ -n $built && -n $head ]] && git -C "$CHAT_USES_REPO" cat-file -e "$built^{commit}" 2>/dev/null; then
	if git -C "$CHAT_USES_REPO" diff --quiet "$built" HEAD -- images/debian-xfce/rootfs images/debian-xfce/provision.sh images/debian-xfce/build.sh; then
		info "image built at $built: the desktop's files are as in the repo"
	else
		info "image built at $built: images/debian-xfce changed since (rebuild, then deploy with --replace for a fresh desktop)"
	fi
elif [[ -n $built ]]; then
	info "image built at $built, which this checkout doesn't have: can't tell if it's current"
fi

# Settings that point bcast or brain somewhere else (Phase 3 and 4 pointed them at the test target).
for service in bcast brain; do
	file=$CHAT_USES_ETC/$service.local.env
	[[ -f $file ]] || continue
	other=$(sed -n 's/^TARGET_HOST=//p' "$file" | tail -1)
	if [[ -n $other && $other != "$TARGET_IP" ]]; then
		warn "$file has TARGET_HOST=$other: $service still uses that machine, not $NAME. To switch:"
		info "sudo sed -i '/^TARGET_HOST=/d' $file && sudo images/$service/deploy.sh"
	fi
done
info "check: images/debian-xfce/check.sh --instance $NAME"
