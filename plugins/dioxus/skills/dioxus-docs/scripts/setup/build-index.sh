#!/usr/bin/env bash
# Build docs.tsv and examples.tsv under $INDEX from the vendored clones.
# Both files are replaced atomically, and only after row-count validation.

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

require_dir "$DIOXUS"
require_dir "$DOCSITE"
mkdir -p "$INDEX"

MIN_DOCS=50
MIN_EXAMPLES=100
tmp="$INDEX/.tmp-$$"
trap 'rm -rf "$tmp"' EXIT
rm -rf "$tmp"
mkdir -p "$tmp"

first_comment() {
    awk '
        /^\/\// {
            line = $0
            sub(/^\/\/[\/!]? ?/, "", line)
            gsub(/\t/, " ", line)
            if (line ~ /[^ ]/) { print substr(line, 1, 200); exit }
            next
        }
        /^[[:space:]]*$/ { next }
        /^#/ { next }
        { exit }
    ' "$1" 2>/dev/null || true
}

first_heading() {
    awk '/^# / { sub(/^# /, ""); gsub(/\t/, " "); print substr($0, 1, 200); exit }' "$1" 2>/dev/null || true
}

build_docs() {
    local slug title
    awk '
        /<!--/ { next }
        {
            while (match($0, /\]\([^)]+\.md\)/)) {
                print substr($0, RSTART + 2, RLENGTH - 3)
                $0 = substr($0, RSTART + RLENGTH)
            }
        }
    ' "$DOCS_ROOT/SUMMARY.md" \
      | LC_ALL=C sort -u \
      | while IFS= read -r rel; do
            [[ -s "$DOCS_ROOT/$rel" ]] || continue
            slug="${rel%.md}"
            title="$(first_heading "$DOCS_ROOT/$rel")"
            printf '%s\t%s\tvendor/docsite/docs-src/0.7/src/%s\n' "$slug" "${title:-${slug##*/}}" "$rel"
        done
}

build_examples() {
    local root="$DIOXUS/examples" entry category name path src
    {
        find "$root" -mindepth 3 -maxdepth 3 -type f -name Cargo.toml
        find "$root" -mindepth 2 -maxdepth 2 -type f -name '*.rs'
    } \
      | awk -v root="$root/" 'index($0, root) == 1 { print substr($0, length(root) + 1) }' \
      | while IFS= read -r entry; do
            category="${entry%%/*}"
            case "$category" in scripts|assets) continue ;; esac
            if [[ "$entry" == */Cargo.toml ]]; then
                path="${entry%/Cargo.toml}"
                name="${path##*/}"
                src="$root/$path/src/main.rs"
                [[ -f "$src" ]] || src="$root/$path/src/lib.rs"
                [[ -f "$src" ]] || src="$(find "$root/$path" -name '*.rs' -type f | LC_ALL=C sort | head -n 1)"
            else
                path="$entry"
                name="$(basename "$entry" .rs)"
                src="$root/$entry"
            fi
            [[ -n "$name" ]] || continue
            printf '%s\t%s\tvendor/dioxus/examples/%s\t%s\n' \
                "$name" "$category" "$path" "$(first_comment "$src")"
        done \
      | LC_ALL=C sort -t $'\t' -k2,2 -k1,1
}

log "[build-index] docs"
build_docs > "$tmp/docs.tsv"
log "[build-index] examples"
build_examples > "$tmp/examples.tsv"

docs_rows=$(awk 'END { print NR }' "$tmp/docs.tsv")
examples_rows=$(awk 'END { print NR }' "$tmp/examples.tsv")
if (( docs_rows <= MIN_DOCS || examples_rows <= MIN_EXAMPLES )); then
    die "index looks wrong (docs=$docs_rows examples=$examples_rows, need more than $MIN_DOCS and $MIN_EXAMPLES); kept the previous index. The clones under $VENDOR may be incomplete."
fi

mv "$tmp/docs.tsv" "$INDEX/docs.tsv"
mv "$tmp/examples.tsv" "$INDEX/examples.tsv"
log "[build-index] done: docs=$docs_rows examples=$examples_rows"
