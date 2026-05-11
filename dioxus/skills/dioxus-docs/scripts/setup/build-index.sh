#!/usr/bin/env bash
# Build the structured index over vendor/dioxus and vendor/docsite.
# Produces four TSVs in $INDEX/. Idempotent.
#
# Usage: build-index.sh

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

require_dir "$DIOXUS"
require_dir "$DOCSITE"
mkdir -p "$INDEX"

log "[build-index] writing TSVs to $INDEX"

#
# files.tsv  —  path \t kind \t one_line_summary
#
log "[build-index] files.tsv"
{
    # Source .rs under packages/*/src
    fd -e rs --type f . "$DIOXUS/packages" 2>/dev/null \
      | awk -v root="$DIOXUS/" 'index($0, root)==1 { sub(root, "vendor/dioxus/", $0); print }' \
      | while IFS= read -r rel; do
            abs="$PLUGIN_ROOT/$rel"
            summary=$(awk '
                /^\/\/!/ { sub(/^\/\/! ?/, ""); if (length($0)>0) {print; exit} }
                /^\/\/\// { sub(/^\/\/\/ ?/, ""); if (length($0)>0) {print; exit} }
            ' "$abs" 2>/dev/null | head -c 200)
            printf '%s\trust\t%s\n' "$rel" "$summary"
        done

    # Markdown under docsite/docs-src/0.7/src
    fd -e md --type f . "$DOCS_ROOT" 2>/dev/null \
      | awk -v root="$DOCSITE/" 'index($0, root)==1 { sub(root, "vendor/docsite/", $0); print }' \
      | while IFS= read -r rel; do
            abs="$PLUGIN_ROOT/$rel"
            summary=$(awk '/^# / { sub(/^# /, ""); print; exit }' "$abs" 2>/dev/null | head -c 200)
            printf '%s\tdoc\t%s\n' "$rel" "$summary"
        done
} > "$INDEX/files.tsv"

#
# examples.tsv  —  name \t category \t path \t one_line_summary
#
log "[build-index] examples.tsv"
fd -e rs --type f . "$DIOXUS/examples" 2>/dev/null \
  | awk -v root="$DIOXUS/" 'index($0, root)==1 { sub(root, "vendor/dioxus/", $0); print }' \
  | while IFS= read -r rel; do
        abs="$PLUGIN_ROOT/$rel"
        # category = first dir under examples/, name = basename minus .rs
        rest="${rel#vendor/dioxus/examples/}"
        category="${rest%%/*}"
        name="$(basename "$rel" .rs)"
        summary=$(awk '
            /^\/\/!/ { sub(/^\/\/! ?/, ""); if (length($0)>0) {print; exit} }
        ' "$abs" 2>/dev/null | head -c 200)
        printf '%s\t%s\t%s\t%s\n' "$name" "$category" "$rel" "$summary"
    done \
  | LC_ALL=C sort -t $'\t' -k2,2 -k1,1 \
  > "$INDEX/examples.tsv"

#
# docs.tsv  —  slug \t title \t path
#
log "[build-index] docs.tsv"
fd -e md --type f . "$DOCS_ROOT" 2>/dev/null \
  | awk -v root="$DOCS_ROOT/" 'index($0, root)==1 { sub(root, "", $0); print }' \
  | while IFS= read -r rel; do
        abs="$DOCS_ROOT/$rel"
        slug="${rel%.md}"
        title=$(awk '/^# / { sub(/^# /, ""); print; exit }' "$abs" 2>/dev/null | head -c 200)
        # Path stored relative to plugin root for consistency with other TSVs
        printf '%s\t%s\tvendor/docsite/docs-src/0.7/src/%s\n' "$slug" "$title" "$rel"
    done \
  | LC_ALL=C sort \
  > "$INDEX/docs.tsv"

log "[build-index] done."
log "  files:    $(wc -l < "$INDEX/files.tsv") rows"
log "  examples: $(wc -l < "$INDEX/examples.tsv") rows"
log "  docs:     $(wc -l < "$INDEX/docs.tsv") rows"
