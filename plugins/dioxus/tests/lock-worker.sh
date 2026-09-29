#!/usr/bin/env bash
set -u
critical=$1 log=$2
scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")/../skills/dioxus-docs/scripts" && pwd)"
# shellcheck source=../skills/dioxus-docs/scripts/_lib.sh
source "$scripts/_lib.sh"
# shellcheck source=../skills/dioxus-docs/scripts/setup/lock.sh
source "$scripts/setup/lock.sh"
acquire_lock
mkdir "$critical" 2>/dev/null || echo OVERLAP >> "$log"
sleep 0.02
rmdir "$critical" 2>/dev/null
echo DONE >> "$log"
