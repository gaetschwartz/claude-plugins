---
name: search
description: Use when searching a repository's code or text — finding definitions, usages, call sites, TODOs, literal strings, or listing files — in a tree where tgrep is installed, instead of the Grep tool or rg/grep. Also use when a tgrep search is slow, misses a file that exists on disk, or prints "no index" / "Server unreachable", and when starting or checking `tgrep index` / `tgrep serve` for a session.
allowed-tools: Bash(tgrep:*)
---

# Searching with tgrep

tgrep is ripgrep with a pre-built trigram index and an optional server. It takes the
common `rg` flags under the same names; an unsupported flag is an error, never silently
ignored. Run `tgrep --help` for the full list.

## Index state (captured when this skill loaded)

!`root=$(git rev-parse --show-toplevel 2>/dev/null || pwd); echo "root: $root"; tgrep status "$root" 2>&1 | head -20 || true`

| Status line | Meaning |
|---|---|
| `No index found` | Every search scans the whole tree. Build one before searching a large repo. |
| `Server: not running` + file counts | On-disk index. Fast, but frozen at the last `tgrep index`. |
| `Server status` + `Indexing: complete` | Server answers in ms and watches for changes. `complete` means the first build finished, not that the index is fresh. |

## Command shape

```
tgrep [flags] -- <pattern> <root>
```

- **All flags before `--`, pattern and path after.** Without `--`, a pattern spelled
  `index`, `serve`, `search`, `status`, `count-files` or `help` is parsed as a subcommand.
- **`<root>` is the directory the index was built for** (normally the repo root). tgrep
  looks for `<root>/.tgrep`; naming a subdirectory or a single file as the path skips the
  index and scans. Scope with `-g`/`-t` instead of narrowing the path.

## Making searches fast

The plugin's SessionStart hook normally starts the server for the repository (the status
above shows it). If it did not — hook disabled, root excluded, or not a git repository — once
per session, from the repo root:

```bash
nohup tgrep serve . >/dev/null 2>&1 &     # builds the index if missing, answers while building, watches for changes
tgrep status .                            # confirm: Server status ... Indexing: complete
```

If a background process cannot be kept alive, `tgrep index .` instead — then re-run it after
any edit a later search must see, including your own. Never commit `.tgrep/`; add it to
`.gitignore` if it is not already ignored.

## Rules of thumb

```bash
tgrep -F -- "Vec<Option<T>>" .              # literal — default for symbols and user-typed strings
tgrep -w -t rust -- handle .                # whole word, one language
tgrep -l -- "impl .* for Server" .          # file names only; then Read the hits
tgrep -g "src/**" -C 2 -- "TODO|FIXME" .    # glob scope, 2 lines of context
tgrep -c -- deprecated .                    # count per file
tgrep --files -t py .                       # list searchable Python files
tgrep --vimgrep -F -- parse_config .        # file:line:col:text, for clickable references
```

- `-F` for anything that is not deliberately a regex; it avoids escaping mistakes.
- Narrow with `-t`/`-g` before reaching for `-m`; scoping is cheap on the index, `-m` only trims output.
- `-l` first on a broad query, then search or Read the specific files.
- `-q` when only yes/no matters; read the exit code (`0` match, `1` none, `2` error).

## Freshness

- **Server:** watcher events apply asynchronously — a search right after an edit can run
  before the index catches up. Pass `--no-index` when the very latest edit must be visible.
- **On-disk index only:** results reflect the last `tgrep index .`; new files are invisible
  until it is re-run. Re-running `tgrep index .` does **not** update a running server.
- `--files` reads the index too; add `--no-index` to list what is on disk right now.

## Flags that force a full scan

Even with a server running: `--hidden`, `--no-ignore` and variants, `-u`/`-uu`/`-uuu`,
`-a`/`--text`, `--binary`, `-E`/`--encoding`, `--no-index`, or a path that is a single file.
`-L`/`--follow`, `--one-file-system` and `--ignore-file` are silently ignored on an indexed
search — pair them with `--no-index`. Avoid all of these on large trees unless needed.

## Reference

`--json` records, keeping `index`/`serve`/search flags aligned (`--exclude`, `--no-ignore`,
`--index-path`, `--max-filesize`, `--no-require-git`), trees without `.git`, and the
troubleshooting table: see `references/reference.md` in this skill's directory.
