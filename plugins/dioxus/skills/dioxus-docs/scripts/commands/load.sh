#!/usr/bin/env bash
# Load a curated topic bundle of Dioxus 0.7 doc pages from the vendored docsite.
#
# Usage: load.sh <topic>
#   Topics: state, ui, fullstack, router, all
#
# Output: a single concatenated markdown stream with per-file headers, suitable
# for front-loading into agent context before writing or reviewing Dioxus code.

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"
ensure_bootstrapped

topic="${1:-all}"
case "$topic" in
    1|hooks|signals|effects|stores|collections|state) topic="state" ;;
    2|rsx|ui)                                          topic="ui" ;;
    3|ssr|server|websockets|fullstack)                 topic="fullstack" ;;
    4|routing|router)                                  topic="router" ;;
    5|all)                                             topic="all" ;;
    -h|--help|help)
        cat >&2 <<'EOF'
Usage: load.sh <topic>
Topics: state, ui, fullstack, router, all
EOF
        exit 0
        ;;
    *) die "unknown topic: $topic (valid: state, ui, fullstack, router, all)" ;;
esac

case "$topic" in
    state)
        files=(
            essentials/basics/hooks.md
            essentials/basics/signals.md
            essentials/basics/effects.md
            essentials/basics/resources.md
            essentials/basics/reactivity.md
            essentials/basics/hoisting.md
            essentials/basics/context.md
            essentials/basics/collections.md
            essentials/advanced/custom_hooks.md
            essentials/advanced/lifecycle.md
        ) ;;
    ui)
        files=(
            essentials/ui/rsx.md
            essentials/ui/elements.md
            essentials/ui/attributes.md
            essentials/ui/conditional.md
            essentials/ui/iteration.md
            essentials/ui/components.md
            essentials/ui/render.md
            essentials/basics/event_handlers.md
        ) ;;
    fullstack)
        files=(
            essentials/fullstack/server_functions.md
            essentials/fullstack/ssr.md
            essentials/fullstack/websockets.md
            essentials/fullstack/streaming.md
            essentials/fullstack/streams.md
            essentials/fullstack/forms.md
            essentials/fullstack/errors.md
            essentials/fullstack/middleware.md
            essentials/fullstack/axum.md
        ) ;;
    router)
        files=(
            essentials/router/routes.md
            essentials/router/navigation.md
            essentials/router/layouts.md
            essentials/router/nested.md
        ) ;;
    all)
        files=(
            essentials/basics/hooks.md
            essentials/basics/signals.md
            essentials/basics/effects.md
            essentials/basics/resources.md
            essentials/basics/hoisting.md
            essentials/basics/context.md
            essentials/basics/event_handlers.md
            essentials/basics/collections.md
            essentials/ui/rsx.md
            essentials/ui/components.md
            essentials/ui/conditional.md
            essentials/ui/iteration.md
        ) ;;
esac

echo "# Dioxus 0.7 Docs — topic: $topic (${#files[@]} files)"
echo

for f in "${files[@]}"; do
    echo "---"
    echo "## $(basename "$f" .md) ($f)"
    echo
    if [[ -f "$DOCS_ROOT/$f" ]]; then
        cat "$DOCS_ROOT/$f"
    else
        log "WARNING: missing vendor/docsite/docs-src/0.7/src/$f"
    fi
    echo
done
