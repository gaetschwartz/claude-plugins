#!/usr/bin/env bash
# ln -s is atomic and its target records the owner pid; dead-owner removal is serialized by TAKEOVER.

LOCK="$DATA/.bootstrap.owner"
TAKEOVER="$DATA/.bootstrap.takeover"
LOCK_TIMEOUT="${LOCK_TIMEOUT:-120}"
LOCK_POLL="${LOCK_POLL:-1}"

lock_owner() { readlink "$1" 2>/dev/null || true; }

lock_release() {
    [[ "$(lock_owner "$1")" != "$$" ]] || rm -f "$1"
}

release_locks() {
    lock_release "$TAKEOVER"
    lock_release "$LOCK"
}

remove_dead_owner() {
    local dead=$1
    ln -s "$$" "$TAKEOVER" 2>/dev/null || return 1
    if [[ "$(lock_owner "$LOCK")" == "$dead" ]] && ! kill -0 "$dead" 2>/dev/null; then
        rm -f "$LOCK"
    fi
    lock_release "$TAKEOVER"
}

acquire_lock() {
    local start=$SECONDS holder announced=0
    mkdir -p "$DATA"
    trap release_locks EXIT
    trap 'exit 1' INT TERM
    until ln -s "$$" "$LOCK" 2>/dev/null; do
        holder="$(lock_owner "$LOCK")"
        if [[ "$holder" =~ ^[0-9]+$ ]] && ! kill -0 "$holder" 2>/dev/null; then
            remove_dead_owner "$holder" && continue
        fi
        (( SECONDS - start < LOCK_TIMEOUT )) \
            || die "timed out after ${LOCK_TIMEOUT}s waiting for another bootstrap (holder pid ${holder:-unknown}); remove $LOCK if it is stale"
        (( announced )) || { log "[bootstrap] another bootstrap is running; waiting"; announced=1; }
        sleep "$LOCK_POLL"
    done
}
