#!/usr/bin/env bash
# Put the stream on air (YouTube gets it) or off (PLAN.md D80). bcast keeps
# running either way: the layout, the encoder, the preview. After a server
# restart the stream is off air until someone puts it back (AIR_AT_BOOT).
#
# Usage:
#   sudo infra/host/onair.sh status
#   sudo infra/host/onair.sh on     after the go-live check (infra/host/golive.sh,
#                                   or golive.sh --go: on air when every check passes)
#   sudo infra/host/onair.sh off    at the end of a session (YouTube ends the
#                                   broadcast if its auto-stop is on)

set -euo pipefail

# shellcheck source=infra/host/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

what=${1:-status}
case $what in
on | off | status) ;;
-h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
*) die "on, off or status" ;;
esac
[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $what"

ready=${CHAT_USES_VAR:-/var/lib/chat-uses}/golive/ready # golive.sh writes it when every check passes
if [[ $what == on ]]; then
	if [[ ! -f $ready ]] || (($(date +%s) - $(stat -c %Y "$ready") > 3600)); then
		warn "the go-live check hasn't passed in the last hour (sudo infra/host/golive.sh): on air all the same"
	fi
fi
if [[ $what == status ]]; then
	incus exec bcast --project infra -- chat-bcast-air status
else
	incus exec bcast --project infra -- chat-bcast-air "$what" "${SUDO_USER:-root} on the host"
fi
