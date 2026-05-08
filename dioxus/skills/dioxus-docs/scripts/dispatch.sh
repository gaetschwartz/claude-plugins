#!/usr/bin/env bash
# Single entry point for the dioxus-docs skill.
#
# All user- and agent-facing access goes through this dispatcher. The
# underlying scripts are an implementation detail.
#
# Bootstrap runs on every invocation (idempotent — fast when vendor present).

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
ensure_bootstrapped

usage() {
    cat >&2 <<'EOF'
Usage: /dioxus-docs <subcommand> [args]

Subcommands:
  search <query> [--scope=docs|src|examples|all] [--limit=N]
                          Smart-case ripgrep across vendored Dioxus + docs.
  read <slug-or-fragment> [--list]
                          Print a Dioxus 0.7 doc page by slug, or list candidates.
  example <pattern> [--list]
                          Find a maintained example under vendor/dioxus/examples/.
  load <topic>            Load a curated bundle of doc pages for a topic.
                          Topics: state, ui, fullstack, router, all.
EOF
}

if (( $# == 0 )); then
    usage
    exit 0
fi

cmd=$1; shift
script_dir="$(dirname "${BASH_SOURCE[0]}")"

case "$cmd" in
    search)         exec bash "$script_dir/search.sh"        "$@" ;;
    read)           exec bash "$script_dir/doc.sh"           "$@" ;;
    example)        exec bash "$script_dir/show-example.sh"  "$@" ;;
    load)           exec bash "$script_dir/load.sh"          "$@" ;;
    -h|--help|help) usage; exit 0 ;;
    *)              log "unknown subcommand: $cmd"; usage; exit 1 ;;
esac
