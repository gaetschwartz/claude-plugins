#!/usr/bin/env bash
# Single entry point for the dioxus-docs command. Usage, help and unknown
# subcommands never touch the network or the data directory.

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

script_dir="$(dirname "${BASH_SOURCE[0]}")"

usage() {
    cat <<EOF
Usage: $PROG <subcommand> [args]

Subcommands:
  search <query> [--scope=docs|src|examples|all] [--limit=N] [--regex]
                          Fixed-string, smart-case ripgrep over the vendored Dioxus and docs.
  semantic <query> [--scope=docs|src|examples|all] [--limit=N]
                          Meaning-based search via semble; needs uv. First use downloads a small model.
  read <slug-or-path> [--list]
                          Print a Dioxus 0.7 doc page (mdbook includes expanded), or list candidates.
                          A path as printed by search/semantic works, with or without .md and :start-end.
  example <pattern> [--list]
                          Find a maintained example under examples/.
  load <topic>            Print a curated bundle of doc pages.
                          Topics: $(bash "$script_dir/commands/load.sh" --names)
                          Run 'load' with no topic for sizes.
  update                  Fetch upstream, reset both clones, rebuild the index.
  paths                   Print absolute vendor, docs, examples and data directories.
  setup-serena            Install rust-analyzer and warm cargo metadata for the Serena MCP server.

Data lives in $DATA.
EOF
}

if (( $# == 0 )); then
    usage
    exit 0
fi

cmd=$1; shift

case "$cmd" in
    search|read|example|semantic|load)
        exec bash "$script_dir/commands/$cmd.sh" "$@" ;;
    update)
        exec bash "$script_dir/setup/bootstrap.sh" update ;;
    setup-serena)
        exec bash "$script_dir/setup/serena.sh" ;;
    paths)
        printf 'vendor=%s\ndocs=%s\nexamples=%s\ndata=%s\n' "$VENDOR" "$DOCS_ROOT" "$DIOXUS/examples" "$DATA"
        if needs_bootstrap; then
            log "not bootstrapped: the first search/read/example/load call clones the repos and builds the index, or run '$PROG update'"
        fi
        exit 0 ;;
    -h|--help|help)
        usage; exit 0 ;;
    *)
        log "unknown subcommand: $cmd"; usage >&2; exit 2 ;;
esac
