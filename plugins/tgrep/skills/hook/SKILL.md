---
name: hook
description: Use when the user wants to turn the automatic tgrep server (the SessionStart hook) off or back on, keep one repository out of it, stop a server it started, asks why a tgrep server appeared in their repo, says tgrep indexing is eating CPU or memory at session start, asks whether the hook is enabled, or wants to check or reset its state.
allowed-tools: Bash(tgrep-serve *)
---

# Controlling the tgrep server hook

## Overview

The plugin ships a `SessionStart` hook that, when the session runs inside a git repository,
starts `tgrep serve <root>` unless `tgrep status <root>` already reports a reachable server,
and a `SessionEnd` hook that stops the server it started once no other tracked session uses
that root (`/clear` keeps it warm). Servers it did not start are used but never stopped.
**It is enabled by default.** It never runs outside a git repository.

Everything it needs lives in one JSON file.

## Current state

!`python3 "${CLAUDE_SKILL_DIR}/../../hooks/tgrep-serve.py" --status 2>&1 || echo "(could not read hook state)"`

Answer from the above rather than re-running `--status`.

## State file

`$XDG_STATE_HOME/tgrep-serve/state.json`, i.e. `~/.local/state/tgrep-serve/state.json` on a
default setup. Override the location with `TGREP_SERVE_STATE`.

```json
{
  "enabled": true,
  "updatedAt": "2026-09-11T07:06:21+00:00",
  "disabledReason": "indexing the monorepo pegs the laptop",
  "excludedRoots": ["/Volumes/DevDisk/src/huge-monorepo"],
  "serveArgs": ["--max-memory", "2048", "--max-cpu", "25"],
  "servers": {
    "/Volumes/DevDisk/src/claude-plugins": {
      "pid": 28047, "port": 61948, "owned": true,
      "startedAt": "…", "sessions": { "<session-id>": "<seenAt>" }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `enabled` | `false` disables the hook. Missing file or missing key means **enabled**. |
| `disabledReason` | Free text, set when disabling; removed on re-enable. |
| `excludedRoots` | Repository roots (real paths) the hook never serves. |
| `serveArgs` | Extra arguments appended to `tgrep serve <root>`, e.g. `--max-memory`, `--max-cpu`, `--exclude`. Must match what searches expect (see the tgrep:search reference). |
| `servers` | One entry per root with a tracked server: `owned` is whether the hook started it (only owned servers are ever stopped); `sessions` are the live sessions using it, pruned after 7 days. |

## Turning it off and on

The hook script doubles as its own CLI, on the `PATH` as `tgrep-serve`:

```bash
tgrep-serve --status
tgrep-serve --disable "reason goes here"
tgrep-serve --enable
tgrep-serve --exclude [root]   # default: the current repository
tgrep-serve --include [root]
tgrep-serve --stop [root]      # stop the server for a root now
```

If `tgrep-serve` isn't available, write the file directly — the schema above is the
whole contract:

```bash
mkdir -p ~/.local/state/tgrep-serve
python3 - <<'PY'
import json, pathlib, datetime
p = pathlib.Path.home() / ".local/state/tgrep-serve/state.json"
s = json.loads(p.read_text()) if p.exists() else {}
s["enabled"] = False          # True to re-enable
s["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
p.write_text(json.dumps(s, indent=2, sort_keys=True) + "\n")
PY
```

Confirm with `--status` afterwards and tell the user which state it ended in. Disabling does
not stop a server that is already running; use `--stop` for that.

## Environment override

`TGREP_SERVE_HOOK` wins over the file when set: `0`/`false`/`no`/`off` disables,
`1`/`true`/`yes`/`on` enables. Useful for one session or one machine without editing shared
state. `--status` reports when an override is in force.

## When it stays silent anyway

Not a malfunction — the hook exits quietly when `tgrep` is not on `PATH`, the session's
working directory is not inside a git repository, or the root is excluded. When it does run
it adds one line of context at session start saying whether it started or found a server.
If a repository has no `.tgrep/` entry in `.gitignore`, that line says so too.
