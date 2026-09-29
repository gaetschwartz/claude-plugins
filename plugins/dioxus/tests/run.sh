#!/usr/bin/env bash
# Offline test suite for the dioxus-docs scripts. Plain bash, no framework.
#
# Usage: tests/run.sh
# Env:   DIOXUS_TEST_VENDOR  directory holding shallow clones dioxus/ and docsite/
#                            (default: fresh shallow clones from GitHub, once)

set -u
export PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$HERE/../skills/dioxus-docs/scripts"
DISPATCH="$SCRIPTS/dispatch.sh"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/dioxus-tests.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0

ok()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  FAIL %s\n' "$1"; [[ -z "${2:-}" ]] || printf '       %s\n' "$2"; }

expect() {
    local name=$1; shift
    if "$@"; then ok "$name"; else bad "$name"; fi
}

rc_is()    { expect "$1" test "$RC" -eq "$2"; }
rc_isnt()  { expect "$1" test "$RC" -ne "$2"; }
out_has()  { expect "$1" contains "$OUT" "$2"; }
out_lacks(){ expect "$1" not contains "$OUT" "$2"; }
err_has()  { expect "$1" contains "$ERR" "$2"; }
err_lacks(){ expect "$1" not contains "$ERR" "$2"; }

contains() { [[ "$1" == *"$2"* ]]; }
not() { ! "$@"; }
lines_of() { printf '%s\n' "$1" | awk 'NF' | wc -l | tr -d ' '; }

# run <data-dir> <args...>: sets OUT, ERR, RC
run() {
    local data=$1; shift
    CLAUDE_PLUGIN_DATA="$data" bash "$DISPATCH" "$@" >"$TMP/out" 2>"$TMP/err"
    RC=$?
    OUT="$(cat "$TMP/out")"
    ERR="$(cat "$TMP/err")"
}

copy_tree() {
    cp -Rc "$1" "$2" 2>/dev/null || cp -R "$1" "$2"
}

prepare_vendor_source() {
    if [[ -n "${DIOXUS_TEST_VENDOR:-}" ]]; then
        VENDOR_SRC="$DIOXUS_TEST_VENDOR"
    else
        VENDOR_SRC="$TMP/vendor-src"
        mkdir -p "$VENDOR_SRC"
        if ! git clone --quiet --depth=1 --branch v0.7 https://github.com/DioxusLabs/dioxus.git "$VENDOR_SRC/dioxus" \
            || ! git clone --quiet --depth=1 https://github.com/DioxusLabs/docsite.git "$VENDOR_SRC/docsite"; then
            echo "cannot obtain vendor clones; set DIOXUS_TEST_VENDOR" >&2
            exit 2
        fi
    fi
    [[ -d "$VENDOR_SRC/dioxus/.git" && -d "$VENDOR_SRC/docsite/.git" ]] \
        || { echo "DIOXUS_TEST_VENDOR must contain dioxus/ and docsite/ clones" >&2; exit 2; }
}

fake_repos() {
    local root=$1 i
    mkdir -p "$root/dioxus/examples/01-demos" "$root/docsite/docs-src/0.7/src/essentials"
    for i in $(seq 1 120); do
        printf '// example number %s\nfn main() {}\n' "$i" > "$root/dioxus/examples/01-demos/ex$i.rs"
    done
    : > "$root/docsite/docs-src/0.7/src/SUMMARY.md"
    for i in $(seq 1 60); do
        printf '# Page %s\n\nbody %s\n' "$i" "$i" > "$root/docsite/docs-src/0.7/src/essentials/p$i.md"
        printf -- '- [Page %s](essentials/p%s.md)\n' "$i" "$i" >> "$root/docsite/docs-src/0.7/src/SUMMARY.md"
    done
    git -C "$root/dioxus" init --quiet -b v0.7
    git -C "$root/docsite" init --quiet -b main
    local r
    for r in dioxus docsite; do
        git -C "$root/$r" add -A
        git -C "$root/$r" -c user.name=t -c user.email=t@t commit --quiet -m init
    done
}

lists_all_subcommands() {
    local s
    for s in search semantic read example load update paths setup-serena; do
        grep -q "^  $s" "$TMP/out" || return 1
    done
}

docs_pages_nonempty() {
    local p
    while IFS= read -r p; do
        [[ -s "$DATA/$p" ]] || return 1
    done < <(cut -f3 "$DATA/index/docs.tsv")
}

no_tmp_under() { [[ -z "$(find "$1" -name '.tmp-*' 2>/dev/null | head -n 1)" ]]; }

echo "usage / help / unknown"
EMPTY="$TMP/empty"
run "$EMPTY"
rc_is "no args exits 0" 0
out_has "no args prints usage" "Usage: dioxus-docs"
out_has "usage lists generated topics" "state"
expect "usage lists every subcommand" lists_all_subcommands
run "$EMPTY" --help
rc_is "--help exits 0" 0
run "$EMPTY" help
rc_is "help exits 0" 0
run "$EMPTY" frobnicate
rc_is "unknown subcommand exits 1" 1
err_has "unknown subcommand prints usage on stderr" "Usage: dioxus-docs"
run "$EMPTY" rag status
rc_is "rag is an unknown subcommand" 1
err_has "rag is reported as unknown" "unknown subcommand: rag"
expect "usage/help/unknown never create vendor" test ! -e "$EMPTY/vendor"

echo "paths (not bootstrapped)"
run "$EMPTY" paths
rc_is "paths exits 0" 0
expect "paths prints the four keys" test "$(printf '%s\n' "$OUT" | cut -d= -f1 | tr '\n' ' ')" = "vendor docs examples data "
out_has "paths reports data dir" "data=$EMPTY"
err_has "paths warns not bootstrapped" "not bootstrapped"
expect "paths does not bootstrap" test ! -e "$EMPTY/vendor"

prepare_vendor_source
DATA="$TMP/data"
mkdir -p "$DATA"
copy_tree "$VENDOR_SRC" "$DATA/vendor"

echo "index build from prepopulated vendor"
run "$DATA" paths
err_has "paths before index warns" "not bootstrapped"
run "$DATA" search zzz-no-such-token-zzz
expect "first call builds docs index" test -s "$DATA/index/docs.tsv"
expect "first call builds examples index" test -s "$DATA/index/examples.tsv"
expect "no files.tsv" test ! -e "$DATA/index/files.tsv"
expect "serena project file written" test -f "$DATA/vendor/dioxus/.serena/project.yml"
expect "no tmp leftovers" no_tmp_under "$DATA"
expect "docs index has no zero-byte pages" docs_pages_nonempty
expect "examples index has unique name+category" test -z "$(cut -f1,2 "$DATA/index/examples.tsv" | sort | uniq -d)"
expect "examples index has no scripts category" test -z "$(cut -f2 "$DATA/index/examples.tsv" | grep -x scripts)"
expect "examples index has no empty names" test -z "$(awk -F'\t' '$1 == ""' "$DATA/index/examples.tsv")"
run "$DATA" paths
expect "paths after index has no warning" test -z "$ERR"
out_has "paths are absolute under data dir" "vendor=$DATA/vendor"

echo "search"
run "$DATA" search 'use_signal(' --scope=src --limit 3
rc_is "fixed-string with paren exits 0" 0
expect "limit applied" test "$(lines_of "$OUT")" -eq 3
err_has "limit truncation noted" "--limit"
err_has "per-file cap noted" "per file"
run "$DATA" search -- --scope=docs
rc_is "-- ends flag parsing: exit 0" 0
err_has "-- ends flag parsing: query is literal" "no matches for: --scope=docs"
run "$DATA" search use_signal --limit abc
rc_isnt "non-numeric --limit rejected" 0
err_has "bad --limit message" "limit"
run "$DATA" search use_signal --limit 0
rc_isnt "zero --limit rejected" 0
run "$DATA" search zzz-no-such-token-zzz
rc_is "no hit exits 0" 0
expect "no hit prints nothing on stdout" test -z "$OUT"
err_has "no hit message on stderr" "no matches for"
run "$DATA" search 'use_signal(' --regex
rc_isnt "--regex surfaces a bad pattern" 0
err_has "--regex error is shown" "regex"
run "$DATA" search 'use_signal\(' --regex --scope=src --limit 2
rc_is "--regex with a valid pattern exits 0" 0
expect "--regex with a valid pattern finds hits" test -n "$OUT"
run "$DATA" search use_signal --scope=docs --limit 100000
out_has "docs scope reaches docs-router doc_examples" "doc_examples"

echo "read"
run "$DATA" read routes
rc_is "read routes exits 0" 0
out_has "read routes prints the page" "# Defining Routes"
out_lacks "read routes has no raw include" '{{#include'
run "$DATA" read tutorial/routing
rc_is "read page with includes exits 0" 0
out_lacks "page with includes: none remain" '{{#include'
out_lacks "page with includes: none missing" "(include missing"
run "$DATA" read --list route
rc_is "--list exits 0" 0
expect "--list prints 3-column TSV candidates" test "$(printf '%s\n' "$OUT" | awk -F'\t' 'NF == 3' | wc -l | tr -d ' ')" -ge 2
run "$DATA" read essentials/router/index
rc_is "full slug exits 0" 0
out_lacks "full slug resolves to a single page" "$(printf '\t')vendor/docsite"
run "$DATA" read index
rc_is "bare 'index' exits 0" 0
run "$DATA" read zzz-no-such-page
rc_is "read no match exits 1" 1

echo "load"
run "$DATA" load
rc_is "load without topic exits 0" 0
out_has "load lists topics" "state"
out_has "load lists sizes" "bytes"
run "$DATA" load nope
rc_is "unknown topic exits 1" 1
err_has "unknown topic says so" "unknown topic"
err_has "unknown topic lists topics" "router"
run "$DATA" load router
rc_is "load router exits 0" 0
out_has "load router prints pages" "# Defining Routes"
err_has "load prints size estimate on stderr" "tokens"
out_lacks "load has no raw include" '{{#include'
run "$DATA" load all
rc_is "'all' topic is gone" 1

echo "example"
run "$DATA" example --list router
rc_is "example --list exits 0" 0
expect "example --list prints 4-column TSV" test "$(printf '%s\n' "$OUT" | awk -F'\t' 'NF >= 4' | wc -l | tr -d ' ')" -ge 1
run "$DATA" example router
rc_is "example exits 0" 0
out_has "example prints paths" "vendor/dioxus/examples/"
run "$DATA" example zzz-no-such-example
rc_is "example no match exits 1" 1

echo "semantic"
STUB="$TMP/stub"
mkdir -p "$STUB"
cat > "$STUB/uvx" <<'STUBEOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$UVX_ARGV"
if [[ -n "${UVX_JSON:-}" ]]; then cat "$UVX_JSON"; else printf '{"query": "q", "results": []}\n'; fi
STUBEOF
chmod +x "$STUB/uvx"
export UVX_ARGV="$TMP/uvx.argv"
argv_has()   { grep -qxF -- "$1" "$UVX_ARGV"; }
argv_lacks() { ! grep -qF -- "$1" "$UVX_ARGV"; }
DOCS_SRC="$DATA/vendor/docsite/docs-src/0.7/src"
DOC_EX="$DATA/vendor/docsite/packages/docs-router/src/doc_examples"
PKGS="$DATA/vendor/dioxus/packages"
EXS="$DATA/vendor/dioxus/examples"

NOBOOT="$TMP/noboot"
PATH="$STUB:$PATH" run "$NOBOOT" semantic
rc_is "semantic without a query exits 2" 2
err_has "semantic without a query prints usage" "usage: dioxus-docs semantic"
expect "semantic without a query never bootstraps" test ! -e "$NOBOOT/vendor"
PATH="$STUB:$PATH" run "$NOBOOT" semantic q --scope=nope
rc_is "semantic invalid scope exits 2" 2
err_has "semantic invalid scope says so" "unknown --scope=nope"
PATH="$STUB:$PATH" run "$NOBOOT" semantic q --limit 0
rc_is "semantic zero --limit exits 2" 2
PATH="$STUB:$PATH" run "$NOBOOT" semantic q --bogus
rc_is "semantic unknown flag exits 2" 2
expect "semantic argument errors never bootstrap" test ! -e "$NOBOOT/vendor"

rm -f "$UVX_ARGV"
PATH="$STUB:$PATH" run "$DATA" semantic "dynamic route segments"
rc_is "semantic default exits 0" 0
expect "semantic default runs semble search" test "$(sed -n '1,4p' "$UVX_ARGV" | tr '\n' ' ')" = "--from semble[mcp] semble search "
expect "semantic passes the query" argv_has "dynamic route segments"
expect "semantic searches all content types" test "$(grep -A1 -x -- '--content' "$UVX_ARGV" | tail -n 1)" = all
expect "semantic default limit is 8" test "$(grep -A1 -x -- '-k' "$UVX_ARGV" | tail -n 1)" = 8
expect "semantic default scope has the book" argv_has "$DOCS_SRC"
expect "semantic default scope has doc_examples" argv_has "$DOC_EX"
expect "semantic default scope excludes framework source" argv_lacks "dioxus/packages"
expect "semantic default scope excludes examples" argv_lacks "dioxus/examples"
err_has "semantic empty result is reported" "no matches for: dynamic route segments"

PATH="$STUB:$PATH" run "$DATA" semantic hooks --scope=src
rc_is "semantic --scope=src exits 0" 0
expect "semantic src has framework source" argv_has "$PKGS"
expect "semantic src excludes the book" argv_lacks "docs-src"
PATH="$STUB:$PATH" run "$DATA" semantic hooks --scope examples
expect "semantic --scope examples has examples" argv_has "$EXS"
expect "semantic --scope examples excludes packages" argv_lacks "dioxus/packages"
PATH="$STUB:$PATH" run "$DATA" semantic hooks --scope=all --limit=3
rc_is "semantic --scope=all exits 0" 0
expect "semantic all has packages" argv_has "$PKGS"
expect "semantic all has examples" argv_has "$EXS"
expect "semantic all has the book" argv_has "$DOCS_SRC"
expect "semantic all has doc_examples" argv_has "$DOC_EX"
expect "semantic --limit=3 maps to -k 3" test "$(grep -A1 -x -- '-k' "$UVX_ARGV" | tail -n 1)" = 3

printf '{"query": "q", "results": [{"file_path": "hooks/src/x.rs", "start_line": 3, "end_line": 9, "score": 0.1, "content": "fn x() {}"}]}\n' > "$TMP/semble.json"
UVX_JSON="$TMP/semble.json" PATH="$STUB:$PATH" run "$DATA" semantic hooks --scope=src
out_has "semantic prints path and line range" "vendor/dioxus/packages/hooks/src/x.rs:3-9"
out_has "semantic prints the snippet" "fn x() {}"

MISSING="$TMP/semantic-missing"
mkdir -p "$MISSING/vendor/dioxus/.git" "$MISSING/vendor/docsite/.git" "$MISSING/index"
printf 'x\n' > "$MISSING/index/docs.tsv"
PATH="$STUB:$PATH" run "$MISSING" semantic hooks --scope=src
rc_isnt "semantic with no existing scope path fails" 0
err_has "semantic missing paths suggest update" "dioxus-docs update"

NOUV="$TMP/nouv-bin"
mkdir -p "$NOUV"
ln -sf "$(command -v bash)" "$NOUV/bash"
ln -sf "$(command -v dirname)" "$NOUV/dirname"
CLAUDE_PLUGIN_DATA="$NOBOOT" PATH="$NOUV" "$NOUV/bash" "$DISPATCH" semantic hooks >"$TMP/out" 2>"$TMP/err"
RC=$?
OUT="$(cat "$TMP/out")"
ERR="$(cat "$TMP/err")"
rc_isnt "semantic without uvx fails" 0
err_has "semantic without uvx names uv" "uv is required"
err_has "semantic without uvx links the install page" "docs.astral.sh/uv"
expect "semantic without uvx never bootstraps" test ! -e "$NOBOOT/vendor"

echo "bootstrap against local repos"
FAKE="$TMP/fake"
mkdir -p "$FAKE"
fake_repos "$FAKE"
export DIOXUS_REPO_URL="file://$FAKE/dioxus" DOCSITE_REPO_URL="file://$FAKE/docsite"

BAD="$TMP/bad"
DIOXUS_REPO_URL="file://$TMP/nonexistent" run "$BAD" search anything
rc_isnt "failed clone exits non-zero" 0
expect "failed clone leaves no repo" test ! -d "$BAD/vendor/dioxus/.git"
expect "failed clone leaves no tmp dir" no_tmp_under "$BAD"
err_has "failed clone message names the repo" "dioxus"
expect "failed clone releases the lock" test ! -e "$BAD/.bootstrap.lock"

RACE="$TMP/race"
mkdir -p "$RACE"
CLAUDE_PLUGIN_DATA="$RACE" bash "$DISPATCH" search "example number 1" >"$TMP/o1" 2>"$TMP/e1" &
p1=$!
CLAUDE_PLUGIN_DATA="$RACE" bash "$DISPATCH" search "example number 2" >"$TMP/o2" 2>"$TMP/e2" &
p2=$!
wait "$p1"; r1=$?
wait "$p2"; r2=$?
expect "concurrent bootstraps both succeed" test "$r1$r2" = 00
expect "concurrent bootstraps: docs index complete" test "$(wc -l < "$RACE/index/docs.tsv" | tr -d ' ')" -eq 60
expect "concurrent bootstraps: examples index complete" test "$(wc -l < "$RACE/index/examples.tsv" | tr -d ' ')" -eq 120
expect "concurrent bootstraps clone dioxus once" test "$(cat "$TMP/e1" "$TMP/e2" | grep -c 'dioxus: cloning')" -eq 1
expect "concurrent bootstraps leave no lock" test ! -e "$RACE/.bootstrap.lock"
expect "concurrent bootstraps leave no tmp dirs" no_tmp_under "$RACE"
expect "fresh bootstrap writes no vendor/.ref or files.tsv" test ! -e "$RACE/vendor/.ref" -a ! -e "$RACE/index/files.tsv"
expect "dioxus cloned at the configured ref" test "$(git -C "$RACE/vendor/dioxus" symbolic-ref --short HEAD)" = v0.7

echo "update"
printf '// brand new example\nfn main() {}\n' > "$FAKE/dioxus/examples/01-demos/fresh.rs"
git -C "$FAKE/dioxus" add -A
git -C "$FAKE/dioxus" -c user.name=t -c user.email=t@t commit --quiet -m more
run "$RACE" update
rc_is "update exits 0" 0
out_has "update prints dioxus short ref" "dioxus=v0.7@"
out_has "update prints docsite short ref" "docsite=main@"
expect "update rebuilds the index" grep -q "^fresh$(printf '\t')" "$RACE/index/examples.tsv"
expect "update leaves no lock" test ! -e "$RACE/.bootstrap.lock"
git -C "$RACE/vendor/dioxus" remote set-url origin "file://$TMP/nonexistent"
run "$RACE" update
rc_isnt "update against a dead remote fails" 0
err_has "update failure names the repo" "dioxus"
expect "failed update keeps the old index" test -s "$RACE/index/docs.tsv"
expect "failed update releases the lock" test ! -e "$RACE/.bootstrap.lock"

echo "index validation"
SHORT="$TMP/short"
mkdir -p "$SHORT"
head -n 20 "$FAKE/docsite/docs-src/0.7/src/SUMMARY.md" > "$TMP/summary"
cp "$TMP/summary" "$FAKE/docsite/docs-src/0.7/src/SUMMARY.md"
git -C "$FAKE/docsite" add -A
git -C "$FAKE/docsite" -c user.name=t -c user.email=t@t commit --quiet -m shrink
run "$SHORT" search anything
rc_isnt "too-small index is rejected" 0
err_has "too-small index explains itself" "index looks wrong"
expect "rejected index is not published" test ! -e "$SHORT/index/docs.tsv"

echo
printf '%s passed, %s failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
