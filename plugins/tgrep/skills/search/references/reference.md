# tgrep reference

Adapted from upstream [AGENTS.md](https://github.com/microsoft/tgrep/blob/main/AGENTS.md);
the [README](https://github.com/microsoft/tgrep#cli-flags) documents every flag.

## How a search resolves

1. **Server** running for this tree — queried over TCP. A file watcher keeps the index close
   to the filesystem; a silently missed notification is repaired only by periodic
   reconciliation (hourly, deferrable up to four hours while queried). `--no-watch` disables it.
2. **On-disk index**, no server — `.tgrep/` is read directly. Only as fresh as the last
   successful publication. If a `serve` was interrupted during its first build, an index
   marked incomplete can remain on disk and a search may use it without warning. Rebuild with
   `tgrep index .` or resume `tgrep serve .` before an exhaustive search.
3. **No index** — every file is scanned, like grep. Correct, slow, and announced on stderr.

A server started with no index answers from an empty index (every search returns nothing)
until the first build completes; one resuming a partial index answers from what it has.
`tgrep status .` shows `Indexing: complete` once the initial build is done — not a freshness
signal, since a server starting on an existing index reconciles in the background while
already reporting complete.

The index is located at `<PATH>/.tgrep` (or `--index-path`). Passing a subdirectory of the
indexed root, or a single file, as the search path means no index is found there and the
search scans. Search from the root and scope with `-g`/`-t`.

## Machine-readable output

`--json` emits one object per line in ripgrep's format: `begin`, `match`, `context`, `end`,
`summary`.

```bash
tgrep --json -F -- "fn main" tgrep-cli/build.rs
```

```json
{"data":{"path":{"text":"tgrep-cli/build.rs"}},"type":"begin"}
{"data":{"absolute_offset":400,"line_number":11,"lines":{"text":"fn main() {\n"},"path":{"text":"tgrep-cli/build.rs"},"submatches":[{"end":7,"match":{"text":"fn main"},"start":0}]},"type":"match"}
{"data":{"binary_offset":null,"path":{"text":"tgrep-cli/build.rs"},"stats":{"bytes_printed":260,"bytes_searched":1775,"elapsed":{"human":"0.000010s","nanos":9709,"secs":0},"matched_lines":1,"matches":1,"searches":1,"searches_with_match":1}},"type":"end"}
{"data":{"elapsed_total":{"human":"0.000470s","nanos":469542,"secs":0},"stats":{"bytes_printed":515,"bytes_searched":1775,"elapsed":{"human":"0.000470s","nanos":469542,"secs":0},"matched_lines":1,"matches":1,"searches":1,"searches_with_match":1}},"type":"summary"}
```

Any `rg --json` parser works, with one exception: on a line that is not valid UTF-8, ripgrep
emits base64 `lines.bytes`; tgrep always emits `lines.text` with each bad byte replaced by
U+FFFD.

`--vimgrep` gives `file:line:col:text`, one row per match.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | At least one match |
| `1` | No match |
| `2` | Error (unreadable path, bad regex, …) |

A match plus an error yields `2`, unless `-q` is set, which yields `0`. Same as ripgrep.
The "no index" warning can accompany code `0` or `1` — always look at stderr.

## Keep `index`, `serve` and search flags aligned

Some flags describe the index. If `index`, `serve` and the search disagree, the client either
cannot find the server or silently searches a different set of files.

| Flag | Must match on |
|---|---|
| `--exclude <DIR>` | `index` and `serve` |
| `--no-ignore` | `index` and `serve`. A server started without it on an index built with it treats the ignored files as deleted and drops them. On a *search* it instead forces a full scan. |
| `--index-path`, `--max-filesize`, `--no-max-filesize`, `--no-require-git` | `index`, `serve` **and every search**. An index built with `--no-max-filesize` but searched with the default cap hides every file above 64 MiB. |

```bash
tgrep index . --index-path /tmp/idx --exclude vendor
tgrep serve . --index-path /tmp/idx --exclude vendor
tgrep --index-path /tmp/idx -- "pattern" .
```

## Trees without `.git`

Plain directories index normally, but like ripgrep tgrep ignores `.gitignore` outside a Git
repository, so the index is larger than expected (tgrep warns). Pass `--no-require-git` to
`index`, `serve` and search to apply the ignore rules anyway.

## `serve` options worth knowing

`tgrep serve --help` has the full list. Notable: `--exclude <DIR>` (repeatable),
`--no-watch`, `--watch-mode poll`, `--poll-interval <s>`, `--max-memory <MB>` and
`--max-cpu <PERCENT>` (initial build defaults to 50 % RAM / 50 % cores),
`--watcher-queue-cap <N>` (raise if branch switches or builds log queue overflows).
The server writes `.tgrep/serve.json` (`{"pid","port"}`) so clients find it.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `warning: no index at … - scanning every file` | No index at the path the search looked in | Search from the indexed root (not a subdirectory or file); if a server or index uses `--index-path`, pass the same value; otherwise `tgrep index .` or `tgrep serve .` |
| `Server unreachable, falling back to local index` | Server died or `serve.json` is stale | Restart `tgrep serve .` |
| New file not found, no server | On-disk index predates the file | `tgrep index .` |
| New file not found, server running | First build in progress, or watcher event still queued | Wait, or `--no-index` for this search; `tgrep index .` does not update a running server |
| Slow despite a server | A flag bypasses the index (see SKILL.md) | Drop the flag or scope with `-g`/`-t` |
| Server indexes its own log | `serve` output redirected into the tree | Redirect to `/dev/null` or outside the tree; `.tgrep/` itself is never indexed |

## Exposing tgrep as a tool

Minimal schema if wrapping tgrep for a model:

```json
{
  "name": "tgrep",
  "description": "Fast regex search over the repository. ripgrep-compatible flags. Use -F for literal strings, -t/-g to scope, -l for file names only, -C N for context.",
  "parameters": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string", "description": "Regex, or literal string with -F"},
      "path": {"type": "string", "default": "."},
      "flags": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["pattern"]
  }
}
```

Canonicalize `path` beneath the repository root and reject escapes; allowlist search-only
flags rather than forwarding arbitrary tokens; run `tgrep <flags...> -- <pattern> <path>` and
return stdout, stderr and the exit code together. `1` is "no results", `2` is an error with
the cause on stderr.
