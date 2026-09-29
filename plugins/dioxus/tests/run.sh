#!/usr/bin/env bash
# DIOXUS_TEST_VENDOR: directory holding shallow clones dioxus/ and docsite/ (default: fetched from GitHub once)

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
rc_is "unknown subcommand exits 2" 2
err_has "unknown subcommand prints usage on stderr" "Usage: dioxus-docs"
run "$EMPTY" rag status
rc_is "rag is an unknown subcommand" 2
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
run "$DATA" search foo -- bar baz
err_has "-- joins words before and after with spaces" "no matches for: foo bar baz"
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
run "$DATA" search use_state --scope=docs --limit 100000
out_lacks "search skips untested_ doc examples" "untested_"
run "$DATA" search use_state --limit 100000
out_lacks "search skips untested_ under every scope" "untested_"
run "$DATA" search use_signal --scope=docs --limit 100000
rc_is "docs search for ordering exits 0" 0
first="$OUT"
expect "search output is sorted by path" test "$(printf '%s\n' "$OUT" | cut -d: -f1 | uniq | LC_ALL=C sort -c 2>&1 | wc -l | tr -d ' ')" -eq 0
for _ in 1 2 3 4; do
    run "$DATA" search use_signal --scope=docs --limit 100000
    [[ "$OUT" == "$first" ]] || break
done
expect "search output is identical across runs" test "$OUT" = "$first"
run "$DATA" search use_signal --scope=docs --limit 5
first="$OUT"
run "$DATA" search use_signal --scope=docs --limit 5
expect "search --limit picks the same hits every time" test "$OUT" = "$first"

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
run "$DATA" read essentials/router/routes
expect "slug read has content" test -n "$OUT"
SLUG_OUT="$OUT"
REL_DOCS="vendor/docsite/docs-src/0.7/src"
for form in "$REL_DOCS/essentials/router/routes.md" "$REL_DOCS/essentials/router/routes" \
            "$REL_DOCS/essentials/router/routes.md:10-20" "$DATA/$REL_DOCS/essentials/router/routes.md" \
            "$DATA/$REL_DOCS/essentials/router/routes.md:7" "essentials/router/routes.md"; do
    run "$DATA" read "$form"
    expect "read accepts path form $form" test "$RC" -eq 0 -a "$OUT" = "$SLUG_OUT"
done

run "$DATA" read migration/to_05/state
out_has "read expands includes from untested_ migration samples" "let state = use_signal(|| 0);"
out_lacks "read migration page has no raw include" '{{#include'
run "$DATA" read 'rout\145s'
rc_is "read query escapes are not interpreted" 1
err_has "read query escapes stay literal" 'no matches for: rout\145s'
run "$DATA" read "routes\\"
rc_is "read query ending in a backslash is a plain miss" 1
run "$DATA" example 'count\145r'
rc_is "example pattern escapes are not interpreted" 1

echo "include containment"
INC="$TMP/inc"
mkdir -p "$INC/site/docs-src/0.7/src" "$INC/site/packages/docs-router/src" "$INC/outside"
printf 'SECRET-OUTSIDE\n' > "$INC/outside/secret.txt"
printf 'INSIDE-OK\n' > "$INC/site/docs-src/0.7/src/ok.txt"
ln -s "$INC/outside/secret.txt" "$INC/site/docs-src/0.7/src/link.txt"
printf 'before\n{{#include ../../../../outside/secret.txt}}\n{{#include link.txt}}\n{{#include ok.txt}}\nafter\n' > "$INC/site/docs-src/0.7/src/page.md"
python3 "$SCRIPTS/lib/expand_includes.py" --docsite "$INC/site" "$INC/site/docs-src/0.7/src/page.md" >"$TMP/out" 2>"$TMP/err"
RC=$?
OUT="$(cat "$TMP/out")"
ERR="$(cat "$TMP/err")"
rc_is "include outside the docsite does not crash" 0
out_lacks "include outside the docsite is not read" "SECRET-OUTSIDE"
out_has "include via a symlink out of the docsite stays unexpanded" "{{#include link.txt}}"
out_has "include with .. out of the docsite stays unexpanded" "{{#include ../../../../outside/secret.txt}}"
out_has "include inside the docsite still expands" "INSIDE-OK"
expect "each rejected include prints one warning" test "$(grep -c '^warning: include outside the docsite' "$TMP/err")" -eq 2

echo "load"
run "$DATA" load
rc_is "load without topic exits 0" 0
out_has "load lists topics" "state"
out_has "load lists sizes" "bytes"
run "$DATA" load nope
rc_is "unknown topic exits 2" 2
err_has "unknown topic says so" "unknown topic"
err_has "unknown topic lists topics" "router"
run "$DATA" load router
rc_is "load router exits 0" 0
out_has "load router prints pages" "# Defining Routes"
err_has "load prints size estimate on stderr" "tokens"
out_lacks "load has no raw include" '{{#include'
run "$DATA" load all
rc_is "'all' topic is gone" 2

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
[[ -z "${UVX_STDERR:-}" ]] || printf '%s\n' "$UVX_STDERR" >&2
if [[ -n "${UVX_JSON:-}" ]]; then cat "$UVX_JSON"; else printf '{"query": "q", "results": []}\n'; fi
exit "${UVX_RC:-0}"
STUBEOF
chmod +x "$STUB/uvx"
export UVX_ARGV="$TMP/uvx.argv"
argv_has()   { grep -qxF -- "$1" "$UVX_ARGV"; }
argv_lacks() { ! grep -qF -- "$1" "$UVX_ARGV"; }
argv_after() { grep -A1 -x -- "$1" "$UVX_ARGV" | tail -n 1; }
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
expect "semantic default runs semble search" test "$(sed -n '1p;3,4p' "$UVX_ARGV" | tr '\n' ' ')" = "--from semble search "
expect "semantic pins semble to an exact version" test "$(sed -n '2p' "$UVX_ARGV" | grep -cE '^semble==[0-9]+\.[0-9]+\.[0-9]+$')" -eq 1
expect "semantic asks for no extras" argv_lacks "[mcp]"
expect "semantic passes the query" argv_has "dynamic route segments"
expect "semantic searches all content types" test "$(grep -A1 -x -- '--content' "$UVX_ARGV" | tail -n 1)" = all
expect "semantic default limit over-fetches 4x" test "$(grep -A1 -x -- '-k' "$UVX_ARGV" | tail -n 1)" = 32
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
expect "semantic --limit=3 over-fetches -k 12" test "$(grep -A1 -x -- '-k' "$UVX_ARGV" | tail -n 1)" = 12
PATH="$STUB:$PATH" run "$DATA" semantic hooks --limit=30
expect "semantic over-fetch is capped at 100" test "$(grep -A1 -x -- '-k' "$UVX_ARGV" | tail -n 1)" = 100
PATH="$STUB:$PATH" run "$DATA" semantic hooks --limit=50
rc_is "semantic accepts the maximum limit" 0
rm -f "$UVX_ARGV"
for bad in 51 3000 99999999999999999999; do
    PATH="$STUB:$PATH" run "$NOBOOT" semantic hooks --limit="$bad"
    rc_is "semantic --limit=$bad exits 2" 2
    err_has "semantic --limit=$bad names the cap" "capped at 50"
done
expect "semantic over-limit never reaches semble" test ! -e "$UVX_ARGV"
PATH="$STUB:$PATH" run "$NOBOOT" semantic '   '
rc_is "semantic whitespace-only query exits 2" 2
expect "semantic whitespace-only query never bootstraps" test ! -e "$NOBOOT/vendor"
err_lacks "semantic prints no first-use notice" "first use"

printf '{"query": "q", "results": [{"file_path": "hooks/src/x.rs", "start_line": 3, "end_line": 9, "score": 0.1, "content": "fn x() {}"}]}\n' > "$TMP/semble.json"
UVX_JSON="$TMP/semble.json" PATH="$STUB:$PATH" run "$DATA" semantic hooks --scope=src
out_has "semantic prints path and line range" "vendor/dioxus/packages/hooks/src/x.rs:3-9"
out_has "semantic prints the snippet" "fn x() {}"

python3 - "$DOC_EX" "$DOCS_SRC" > "$TMP/stale.json" <<'PY'
import json, sys
ex, src = sys.argv[1:3]
hits = [
    ("doc_examples/untested_04/a.rs", "STALE04"),
    ("doc_examples/nav.rs", "KEEP1"),
    ("doc_examples/untested_06/b.rs", "STALE06"),
    ("src/essentials/router/navigation.md", "KEEP2"),
    ("doc_examples/routes.rs", "KEEP3"),
]
results = [{"file_path": p, "start_line": 1, "end_line": 2, "score": 0.1, "content": c} for p, c in hits]
print(json.dumps({"query": "q", "results": results, "repos": {"doc_examples": ex, "src": src}}))
PY
UVX_JSON="$TMP/stale.json" PATH="$STUB:$PATH" run "$DATA" semantic q --limit=2
rc_is "semantic with stale hits exits 0" 0
out_lacks "semantic drops untested_04 hits" "STALE04"
out_lacks "semantic drops untested_06 hits" "STALE06"
out_has "semantic keeps the first current hit" "KEEP1"
out_has "semantic keeps the second current hit" "KEEP2"
out_lacks "semantic honours the limit after filtering" "KEEP3"
UVX_JSON="$TMP/stale.json" PATH="$STUB:$PATH" run "$DATA" semantic q --limit=5
out_has "semantic returns all current hits when fewer than the limit" "KEEP3"

PATH="$STUB:$PATH" run "$DATA" semantic 'plain words'
expect "semantic puts -- directly before the query" test "$(argv_after --)" = "plain words"
PATH="$STUB:$PATH" run "$DATA" semantic -- -foo
rc_is "semantic -- -foo exits 0" 0
expect "semantic -- -foo passes the query after --" test "$(argv_after --)" = "-foo"
PATH="$STUB:$PATH" run "$DATA" semantic -- --release
rc_is "semantic -- --release exits 0" 0
expect "semantic -- --release passes the query after --" test "$(argv_after --)" = "--release"

printf '{"query": "q", "results": [{"file_path": "gone/x.rs", "start_line": 1, "end_line": 2, "content": "LOST"}, {"file_path": "hooks/src/nocontent.rs", "start_line": 1, "end_line": 2}, {"file_path": "hooks/src/ok.rs", "start_line": 4, "end_line": 5, "content": "FINE"}], "repos": {"hooks": "%s"}}\n' "$PKGS" > "$TMP/partial.json"
UVX_JSON="$TMP/partial.json" PATH="$STUB:$PATH" run "$DATA" semantic q --scope=src
rc_is "semantic tolerates unresolved labels and missing content" 0
out_has "semantic keeps well-formed hits beside malformed ones" "FINE"
out_lacks "semantic skips hits with an unresolved label" "LOST"
out_lacks "semantic skips hits without content" "nocontent"
err_lacks "semantic malformed hits produce no traceback" "Traceback"

printf 'this is not json\n' > "$TMP/garbage.json"
UVX_JSON="$TMP/garbage.json" PATH="$STUB:$PATH" run "$DATA" semantic q
rc_isnt "semantic with bad JSON fails" 0
err_has "semantic bad JSON is reported" "semble returned unexpected output"
err_lacks "semantic bad JSON has no traceback" "Traceback"
expect "semantic bad JSON error is short" test "$(lines_of "$ERR")" -le 3

UVX_RC=3 UVX_STDERR="boom from uv" PATH="$STUB:$PATH" run "$DATA" semantic q
rc_isnt "semantic with a failing uvx fails" 0
err_has "semantic surfaces semble's own stderr" "boom from uv"
err_has "semantic names the failing step" "semble search failed"
err_lacks "semantic uvx failure has no traceback" "Traceback"
expect "semantic uvx failure prints no results" test -z "$OUT"
UVX_STDERR="WARNING: Language Foo not found, skipping" PATH="$STUB:$PATH" run "$DATA" semantic q
err_lacks "semantic drops semble's language warnings" "WARNING: Language"
UVX_STDERR="something else" PATH="$STUB:$PATH" run "$DATA" semantic q
err_has "semantic passes other semble stderr through" "something else"

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

echo "argument errors never bootstrap"
DEAD_URLS=(DIOXUS_REPO_URL=file:///nonexistent DOCSITE_REPO_URL=file:///nonexistent)
expect_no_bootstrap() {
    local name=$1 rc_want=$2; shift 2
    local d
    d="$(mktemp -d "$TMP/nb.XXXXXX")"
    env "${DEAD_URLS[@]}" CLAUDE_PLUGIN_DATA="$d" bash "$DISPATCH" "$@" >"$TMP/out" 2>"$TMP/err"
    RC=$?
    OUT="$(cat "$TMP/out")"
    ERR="$(cat "$TMP/err")"
    expect "$name: exit $rc_want" test "$RC" -eq "$rc_want"
    expect "$name: no vendor dir" test ! -e "$d/vendor"
}
expect_no_bootstrap "read --help" 0 read --help
out_has "read --help prints usage" "Usage: dioxus-docs read"
expect_no_bootstrap "read -h" 0 read -h
expect_no_bootstrap "read --bogus" 2 read --bogus
err_has "read --bogus names the flag" "unknown flag: --bogus"
expect_no_bootstrap "read without a query" 2 read
expect_no_bootstrap "example -h" 0 example -h
out_has "example -h prints usage" "Usage: dioxus-docs example"
expect_no_bootstrap "example --bogus" 2 example --bogus
expect_no_bootstrap "load nope" 2 load nope
err_has "load nope lists topics" "topics:"
expect_no_bootstrap "load a b" 2 load 'a b'
expect_no_bootstrap "load ../x" 2 load ../x
expect_no_bootstrap "load --list" 2 load --list
expect_no_bootstrap "read whitespace-only query" 2 read '   '
expect_no_bootstrap "example whitespace-only query" 2 example ' '
expect_no_bootstrap "search whitespace-only query" 2 search '  '
expect_no_bootstrap "search without a query" 2 search
expect_no_bootstrap "search --scope=nope" 2 search q --scope=nope
expect_no_bootstrap "search --limit abc" 2 search q --limit abc
expect_no_bootstrap "search --limit 0" 2 search q --limit 0
expect_no_bootstrap "search --bogus" 2 search --bogus
expect_no_bootstrap "example without a query" 2 example
expect_no_bootstrap "search --help" 0 search --help
expect_no_bootstrap "semantic --help" 0 semantic --help
run "$DATA" read --help
out_has "read usage names the slug-or-path argument" "read <slug-or-path>"
out_has "read usage lists the accepted path forms" "as printed by search/semantic"
run "$DATA" read --bogus
rc_is "read --bogus on a bootstrapped dir exits 2" 2
run "$DATA" read -- --bogus
err_has "read -- treats the rest as the query" "no matches for: --bogus"
run "$DATA" example -- --bogus
err_has "example -- treats the rest as the query" "no matches for: --bogus"

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
err_has "failed clone reports the bootstrap failure" "bootstrap failed"
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

echo "bootstrap locking"
LIVE="$TMP/live-holder"
mkdir -p "$LIVE"
sleep 60 &
holder=$!
ln -s "$holder" "$LIVE/.bootstrap.owner"
LOCK_TIMEOUT=2 run "$LIVE" search anything
rc_isnt "waiting on a live holder times out" 0
err_has "timeout message names the holder" "timed out after 2s"
expect "timed-out waiter leaves the live holder's lock" test "$(readlink "$LIVE/.bootstrap.owner" 2>/dev/null)" = "$holder"
kill "$holder" 2>/dev/null
wait "$holder" 2>/dev/null

STALE="$TMP/stale-holder"
mkdir -p "$STALE"
true &
dead=$!
wait "$dead"
ln -s "$dead" "$STALE/.bootstrap.owner"
run "$STALE" search "example number 1"
rc_is "a dead holder's lock is taken over" 0
expect "takeover leaves no lock files" test -z "$(find "$STALE" -maxdepth 1 -name '.bootstrap.*')"

GUARDED="$TMP/guarded"
mkdir -p "$GUARDED"
sleep 60 &
taker=$!
ln -s "$dead" "$GUARDED/.bootstrap.owner"
ln -s "$taker" "$GUARDED/.bootstrap.takeover"
CLAUDE_PLUGIN_DATA="$GUARDED" LOCK_POLL=0.05 LOCK_TIMEOUT=1 bash "$HERE/lock-worker.sh" "$GUARDED/critical" "$GUARDED/log" 2>/dev/null
rc_isnt_zero=$?
expect "a waiter does not remove a dead lock while another takeover is in progress" test "$rc_isnt_zero" -ne 0 -a "$(readlink "$GUARDED/.bootstrap.owner")" = "$dead"
kill "$taker" 2>/dev/null
wait "$taker" 2>/dev/null

WAITERS=8
STRESS_RUNS=20
stress_fail=0
stress_kept=0
for round in $(seq "$STRESS_RUNS"); do
    SD="$TMP/stress-$round"
    mkdir -p "$SD"
    ln -s "$dead" "$SD/.bootstrap.owner"
    : > "$SD/log"
    pids=()
    for _ in $(seq "$WAITERS"); do
        CLAUDE_PLUGIN_DATA="$SD" LOCK_POLL=0.02 bash "$HERE/lock-worker.sh" "$SD/critical" "$SD/log" 2>/dev/null &
        pids+=($!)
    done
    for p in "${pids[@]}"; do wait "$p" || stress_fail=$((stress_fail + 1)); done
    [[ "$(grep -c '^DONE$' "$SD/log")" -eq "$WAITERS" && "$(grep -c OVERLAP "$SD/log")" -eq 0 ]] || stress_fail=$((stress_fail + 1))
    [[ -z "$(find "$SD" -maxdepth 1 -name '.bootstrap.*')" ]] || stress_fail=$((stress_fail + 1))

    sleep 60 &
    live=$!
    rm -f "$SD/.bootstrap.owner"
    ln -s "$live" "$SD/.bootstrap.owner"
    pids=()
    for _ in $(seq "$WAITERS"); do
        CLAUDE_PLUGIN_DATA="$SD" LOCK_POLL=0.02 LOCK_TIMEOUT=1 bash "$HERE/lock-worker.sh" "$SD/critical" "$SD/log2" 2>/dev/null &
        pids+=($!)
    done
    for p in "${pids[@]}"; do wait "$p" || true; done
    [[ "$(readlink "$SD/.bootstrap.owner")" == "$live" ]] || stress_kept=$((stress_kept + 1))
    kill "$live" 2>/dev/null
    wait "$live" 2>/dev/null
done
expect "stale lock takeover admits one waiter at a time across $STRESS_RUNS rounds of $WAITERS waiters" test "$stress_fail" -eq 0
expect "no waiter removes a live holder's lock across $STRESS_RUNS rounds" test "$stress_kept" -eq 0

TMPD="$TMP/tmp-cleanup"
mkdir -p "$TMPD/vendor"
sleep 60 &
alive=$!
mkdir "$TMPD/vendor/.tmp-dioxus-$alive" "$TMPD/vendor/.tmp-dioxus-$dead" "$TMPD/vendor/.tmp-odd-name"
run "$TMPD" search "example number 1"
rc_is "bootstrap with leftover temp dirs succeeds" 0
expect "temp dir of a live pid is kept" test -d "$TMPD/vendor/.tmp-dioxus-$alive"
expect "temp dir of a dead pid is removed" test ! -e "$TMPD/vendor/.tmp-dioxus-$dead"
expect "temp dir without a pid suffix is removed" test ! -e "$TMPD/vendor/.tmp-odd-name"
kill "$alive" 2>/dev/null
wait "$alive" 2>/dev/null

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
