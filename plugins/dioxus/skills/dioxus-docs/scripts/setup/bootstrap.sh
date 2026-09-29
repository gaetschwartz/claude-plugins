#!/usr/bin/env bash
# Clone or refresh the vendored Dioxus + docsite repos and rebuild the index.
#
# Usage: bootstrap.sh first-run|update
#   first-run  clone whatever is missing, write the Serena project file, build the index.
#   update     fetch and hard-reset both clones to upstream, then rebuild the index.

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"
# shellcheck source=lock.sh
source "$_LIB_DIR/setup/lock.sh"

mode="${1:-first-run}"
[[ "$mode" == first-run || "$mode" == update ]] || die "usage: bootstrap.sh first-run|update"

remove_dead_tmp() {
    local d pid
    for d in "$VENDOR"/.tmp-*; do
        [[ -e "$d" ]] || continue
        pid="${d##*-}"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            continue
        fi
        rm -rf "$d"
    done
}

clone_repo() {
    local name=$1 url=$2 ref=$3 dest=$4
    local tmp="$VENDOR/.tmp-$name-$$"
    local args=(--depth=1)
    [[ -z "$ref" ]] || args+=(--branch "$ref")
    rm -rf "$tmp" "$dest"
    log "[bootstrap] $name: cloning ${ref:-default branch} from $url"
    if ! git clone "${args[@]}" --quiet -- "$url" "$tmp" >&2; then
        rm -rf "$tmp"
        die "cloning $name from $url${ref:+ at ref $ref} failed; nothing was installed"
    fi
    mv "$tmp" "$dest"
}

reset_repo() {
    local name=$1 ref=$2 dir=$3
    log "[bootstrap] $name: fetching ${ref:-default branch}"
    git -C "$dir" fetch --quiet --depth=1 origin "${ref:-HEAD}" >&2 \
        || die "fetching $name failed"
    git -C "$dir" reset --quiet --hard FETCH_HEAD >&2
}

prune_docsite_assets() {
    find "$DOCSITE/packages" -maxdepth 2 -type d -name assets -exec rm -rf {} + 2>/dev/null || true
}

write_serena_project() {
    [[ ! -f "$DIOXUS/.serena/project.yml" ]] || return 0
    mkdir -p "$DIOXUS/.serena"
    cat > "$DIOXUS/.serena/project.yml" <<'YAML'
project_name: dioxus
language_servers:
  - rust
read_only: true
ignored_paths:
  - target
  - .git
  - .github
  - flake.lock
  - Cargo.lock
  - notes
  - playwright-tests
YAML
}

short_ref() {
    printf '%s@%s\n' "$1" "$(git -C "$2" rev-parse --short HEAD)"
}

command -v git >/dev/null || die "git is required"

acquire_lock
mkdir -p "$VENDOR"
remove_dead_tmp

if [[ "$mode" == first-run ]] && ! needs_bootstrap; then
    exit 0
fi

if [[ ! -d "$DIOXUS/.git" ]]; then
    clone_repo dioxus "$DIOXUS_REPO_URL" "$DIOXUS_REF" "$DIOXUS"
elif [[ "$mode" == update ]]; then
    reset_repo dioxus "$DIOXUS_REF" "$DIOXUS"
fi

if [[ ! -d "$DOCSITE/.git" ]]; then
    clone_repo docsite "$DOCSITE_REPO_URL" "" "$DOCSITE"
elif [[ "$mode" == update ]]; then
    reset_repo docsite "" "$DOCSITE"
fi

prune_docsite_assets
write_serena_project
bash "$_LIB_DIR/setup/build-index.sh" >&2

if [[ "$mode" == update ]]; then
    printf 'dioxus=%s\n' "$(short_ref "$DIOXUS_REF" "$DIOXUS")"
    printf 'docsite=%s\n' "$(short_ref "$(git -C "$DOCSITE" symbolic-ref --short -q HEAD || echo HEAD)" "$DOCSITE")"
fi
