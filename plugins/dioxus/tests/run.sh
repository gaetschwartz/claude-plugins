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
    for s in search read example load update paths setup-serena rag; do
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

echo "rag (offline)"
unset OPENAI_API_KEY
RAG_DIR="$SCRIPTS/rag"
RAGD="$TMP/rag"
mkdir -p "$RAGD"
file_mode() { python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }
printf 'sk-stdin-secret\n' > "$TMP/key"
run "$RAGD" rag config set-openai-key < "$TMP/key"
rc_is "set-openai-key reads stdin" 0
expect "secret file is 0600" test "$(file_mode "$RAGD/.rag-config-secrets")" = 600
expect "secret file holds the key" grep -q 'sk-stdin-secret' "$RAGD/.rag-config-secrets"
expect "key is never echoed" not contains "$OUT$ERR" "sk-stdin-secret"
chmod 644 "$RAGD/.rag-config-secrets"
run "$RAGD" rag config set-openai-key < "$TMP/key"
expect "rewriting tightens the secret mode" test "$(file_mode "$RAGD/.rag-config-secrets")" = 600
expect "no temp files left behind" test -z "$(find "$RAGD" -name '*.tmp-*')"
run "$RAGD" rag config set-openai-key sk-on-argv < "$TMP/key"
rc_isnt "set-openai-key refuses argv" 0
err_has "argv refusal says to use stdin" "stdin"
expect "argv key is not stored" not grep -q 'sk-on-argv' "$RAGD/.rag-config-secrets"
ENVD="$TMP/rag-env"
mkdir -p "$ENVD"
run "$ENVD" rag config set-openai-key < /dev/null
rc_isnt "set-openai-key without input fails" 0
err_has "missing key is explained" "no key provided"
expect "failed set-openai-key writes no secret" test ! -e "$ENVD/.rag-config-secrets"
OPENAI_API_KEY=sk-from-env run "$ENVD" rag config set-openai-key < /dev/null
rc_is "set-openai-key falls back to the env var" 0
expect "env key stored 0600" test "$(file_mode "$ENVD/.rag-config-secrets")" = 600
run "$RAGD" rag config set-trust-remote-code on
rc_is "set-trust-remote-code on" 0
run "$RAGD" rag config show
out_has "config show reports trust_remote_code on" "trust_remote_code**: \`true\`"
run "$RAGD" rag config set-trust-remote-code maybe
rc_isnt "set-trust-remote-code rejects other values" 0
run "$RAGD" rag config set-trust-remote-code off
run "$RAGD" rag config show
out_has "trust_remote_code off again" "trust_remote_code**: \`false\`"
run "$RAGD" rag rebuild docs
rc_isnt "rag rebuild is gone" 0
err_has "rag rebuild is an unknown verb" "unknown rag verb"
run "$RAGD" rag help
err_lacks "usage no longer lists rebuild" "rebuild"
err_has "usage documents --force" "--force"
run "$RAGD" rag status
rc_is "status without a venv is a no-op" 0
err_has "status explains the missing venv" "disabled"
run "$RAGD" rag query anything
rc_isnt "query without a venv fails" 0
err_has "query says RAG is not enabled" "RAG is not enabled"
IDX="$TMP/rag-indexed"
mkdir -p "$IDX"
printf '{"books": {"docs": {"backend": "ollama", "model": "m", "indexed_at": "2026-01-01T00:00:00Z"}}}\n' > "$IDX/.rag-state.json"
run "$IDX" rag enable docs
rc_is "enable on an indexed book is a no-op" 0
err_has "enable points to --force" "--force"
expect "no-op enable creates no venv" test ! -e "$IDX/.rag-venv"
expect "no-op enable clones nothing" test ! -e "$IDX/vendor"
run "$IDX" rag enable nonsense
rc_isnt "enable rejects unknown books" 0
python3 - "$RAG_DIR" "$TMP/atomic" <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import state
data = Path(sys.argv[2])
data.mkdir()
state.save_state(data, {"books": {"docs": {"backend": "ollama"}}})
before = state.state_path(data).read_text()
real_replace = os.replace
def boom(*a, **k):
    raise OSError("simulated crash")
os.replace = boom
try:
    state.save_state(data, {"books": {"docs": {"backend": "openai"}}})
    print("no-error")
except OSError:
    pass
os.replace = real_replace
assert state.state_path(data).read_text() == before, "state changed by a failed write"
leftovers = [p.name for p in data.iterdir() if ".tmp-" in p.name]
assert not leftovers, leftovers
json.loads(before)
PY
RC=$?
expect "state writes are atomic and clean up" test "$RC" -eq 0
CLAUDE_PLUGIN_DATA="$TMP/venvarg" bash "$SCRIPTS/setup/rag-venv.sh" --bogus >/dev/null 2>&1
RC=$?
rc_isnt "rag-venv.sh rejects unknown flags" 0

echo
printf '%s passed, %s failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
