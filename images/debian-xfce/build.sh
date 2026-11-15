#!/usr/bin/env bash
# Build the target desktop image: same as `images/build.sh debian-xfce`.
exec "$(dirname "${BASH_SOURCE[0]}")/../build.sh" debian-xfce "$@"
