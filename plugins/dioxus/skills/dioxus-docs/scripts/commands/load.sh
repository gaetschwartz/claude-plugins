#!/usr/bin/env bash
# Usage: load.sh [<topic>]
#   With a topic: prints the topic's pages as one markdown stream (mdbook includes expanded).
#   Without: lists topics with size estimates. Unknown topic: same list, exit 1.
# A topic is any topic_<name> function below.

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

topic_state() {
    files=(
        essentials/basics/hooks.md
        essentials/basics/signals.md
        essentials/basics/effects.md
        essentials/basics/resources.md
        essentials/basics/reactivity.md
        essentials/basics/hoisting.md
        essentials/basics/context.md
        essentials/basics/collections.md
        essentials/basics/async.md
        essentials/basics/error_handling.md
        essentials/basics/suspense.md
        essentials/advanced/custom_hooks.md
        essentials/advanced/lifecycle.md
    )
}

topic_ui() {
    files=(
        essentials/ui/rsx.md
        essentials/ui/elements.md
        essentials/ui/attributes.md
        essentials/ui/conditional.md
        essentials/ui/iteration.md
        essentials/ui/components.md
        essentials/ui/render.md
        essentials/basics/event_handlers.md
    )
}

topic_fullstack() {
    files=(
        essentials/fullstack/project_setup.md
        essentials/fullstack/server_functions.md
        essentials/fullstack/ssr.md
        essentials/fullstack/websockets.md
        essentials/fullstack/streaming.md
        essentials/fullstack/streams.md
        essentials/fullstack/forms.md
        essentials/fullstack/errors.md
        essentials/fullstack/middleware.md
        essentials/fullstack/axum.md
        essentials/fullstack/authentication.md
        essentials/fullstack/native.md
    )
}

topic_router() {
    files=(
        essentials/router/routes.md
        essentials/router/navigation.md
        essentials/router/layouts.md
    )
}

list_names() {
    compgen -A function topic_ | sed 's/^topic_//'
}

case "${1:-}" in
    --names)
        list_names | paste -sd, - | sed 's/,/, /g'
        exit 0 ;;
    -h|--help|help)
        printf 'Usage: %s load [<topic>]\nTopics: %s\n' "$PROG" "$(list_names | paste -sd, - | sed 's/,/, /g')"
        exit 0 ;;
esac

topic="${1:-}"
if [[ "$topic" == -* ]]; then
    usage_error "unknown flag: $topic (usage: $PROG load [<topic>])"
fi
if [[ -n "$topic" ]] && { ! [[ "$topic" =~ ^[a-z]+$ ]] || ! declare -F "topic_$topic" >/dev/null; }; then
    log "unknown topic: $topic"
    usage_error "topics: $(list_names | paste -sd, - | sed 's/,/, /g')"
fi

ensure_bootstrapped

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

build_bundle() {
    local topic=$1 f
    files=()
    "topic_$topic"
    for f in "${files[@]}"; do
        [[ -f "$DOCS_ROOT/$f" ]] || die "topic '$topic' lists a page that is missing from the docs clone: $f"
    done
    {
        printf '# Dioxus 0.7 Docs, topic: %s (%d pages)\n\n' "$topic" "${#files[@]}"
        for f in "${files[@]}"; do
            printf -- '---\n## %s (%s)\n\n' "$(basename "$f" .md)" "$f"
            print_page "$DOCS_ROOT/$f"
            printf '\n'
        done
    } > "$tmp/$topic.md"
}

describe() {
    local bytes
    bytes=$(( $(wc -c < "$tmp/$1.md") ))
    printf '%s\t%d bytes\t~%d tokens\t%d pages' "$1" "$bytes" $(( bytes / 4 )) "${#files[@]}"
}

list_topics() {
    local t
    for t in $(list_names); do
        build_bundle "$t"
        describe "$t"
        printf '\n'
    done
}

if [[ -z "$topic" ]]; then
    list_topics
    exit 0
fi

build_bundle "$topic"
log "[load] $(describe "$topic")"
cat "$tmp/$topic.md"
