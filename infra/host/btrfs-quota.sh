#!/usr/bin/env bash
# Keep btrfs quota accounting right on Incus's storage pool (PLAN.md D51).
#
# Incus limits each instance's disk (the root device's `size`) with btrfs
# qgroups. When the kernel deletes a subvolume whose tree is deeper than
# drop_subtree_threshold (3 by default), it skips counting and marks every
# count "inconsistent": from then on instances look empty and no limit ever
# trips. Found on the server: a target wrote 6 GiB past its 4 GiB.
#
# This makes deletions always counted (threshold 8), turns quotas on if
# Incus hasn't yet, and recounts when the counts are broken anyway.
# Installed as /usr/local/sbin/chat-uses-btrfs-quota by bootstrap.sh (step
# quotas); runs at boot and every 6 hours (chat-uses-btrfs-quota.timer).
#
# Usage: chat-uses-btrfs-quota POOL_PATH   (e.g. /var/lib/incus/storage-pools/chat)
# Exit status: 0 when the counts are consistent.

set -euo pipefail

pool=${1:?usage: chat-uses-btrfs-quota POOL_PATH}
for _ in $(seq 1 90); do # Incus mounts the pool when it starts
	mountpoint -q "$pool" && break
	sleep 2
done
mountpoint -q "$pool" || { echo "$pool isn't mounted (is Incus running?)" >&2; exit 1; }
uuid=$(btrfs filesystem show "$pool" | awk '/uuid:/ {print $NF; exit}')
q=/sys/fs/btrfs/$uuid/qgroups

if [[ ! -d $q ]]; then
	echo "quotas were off on $pool: turning them on (Incus's disk limits need them)"
	btrfs quota enable "$pool"
	btrfs quota rescan -w "$pool" >/dev/null 2>&1 || true
fi
if [[ $(cat "$q/drop_subtree_threshold") != 8 ]]; then
	echo 8 >"$q/drop_subtree_threshold"
	echo "deleting an instance now keeps the counts right (drop_subtree_threshold 8)"
fi
if [[ $(cat "$q/inconsistent") == 1 ]]; then
	echo "quota counts were inconsistent: recounting (disk limits weren't holding)"
	btrfs quota rescan -w "$pool" >/dev/null
fi
if [[ $(cat "$q/inconsistent") == 1 ]]; then
	echo "quota counts still inconsistent after a recount: disk limits don't hold" >&2
	exit 1
fi
echo "quota counts consistent: disk limits hold"
