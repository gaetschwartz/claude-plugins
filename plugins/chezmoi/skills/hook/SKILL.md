---
name: hook
description: Use when the user wants to turn the chezmoi drift advisor off or back on, asks why they keep getting chezmoi drift notices, says the chezmoi hook is noisy or annoying, asks whether the drift hook is enabled, or wants to check or reset its state.
allowed-tools: Bash(chezmoi-drift *)
---

# Controlling the chezmoi drift hook

## Overview

The plugin snapshots `chezmoi status` before and after each `Bash`, `Edit`, `Write` and
`NotebookEdit` call, and tells the agent which managed files **that call** left out of sync.
**It is enabled by default.** Drift that already existed, or that another session caused,
is not reported; a path is reported once per session until it stops drifting.

## Current state

!`python3 "${CLAUDE_SKILL_DIR}/../../hooks/chezmoi-drift.py" --status 2>&1 || echo "(could not read hook state)"`

Answer from the above rather than re-running `--status`.

## State file

`$XDG_STATE_HOME/chezmoi-drift/state.json`, i.e. `~/.local/state/chezmoi-drift/state.json`
on a default setup. Override the location with `CHEZMOI_DRIFT_STATE`.

```json
{
  "enabled": true,
  "updatedAt": "2026-09-03T07:03:39+00:00",
  "disabledReason": "too noisy during refactor"
}
```

| Field | Meaning |
|---|---|
| `enabled` | `false` disables the hook. Missing file or missing key means **enabled**. |
| `disabledReason` | Free text, set when disabling; removed on re-enable. |

Next to it, the hook keeps working files it manages itself:

| Path | Contents |
|---|---|
| `windows/<tool-use-id>.json` | One tool call's session, target, start/end time and pre-call snapshot. Used to tell which session changed a file when two were running at once. Pruned after 10 minutes. |
| `sessions/<session-id>.json` | Paths already reported to that session. Pruned after 7 days. |

## Turning it off and on

The hook script doubles as its own CLI, on the `PATH` as `chezmoi-drift`:

```bash
chezmoi-drift --status
chezmoi-drift --disable "reason goes here"
chezmoi-drift --enable
```

If `chezmoi-drift` isn't available, write the file directly — the schema above is the
whole contract:

```bash
mkdir -p ~/.local/state/chezmoi-drift
python3 - <<'PY'
import json, pathlib, datetime
p = pathlib.Path.home() / ".local/state/chezmoi-drift/state.json"
s = json.loads(p.read_text()) if p.exists() else {}
s["enabled"] = False          # True to re-enable
s["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
p.write_text(json.dumps(s, indent=2, sort_keys=True) + "\n")
PY
```

Confirm with `--status` afterwards and tell the user which state it ended in.

## Environment override

`CHEZMOI_DRIFT_HOOK` wins over the file when set: `0`/`false`/`no`/`off` disables,
`1`/`true`/`yes`/`on` enables. Useful for one session or one machine without editing shared
state. `--status` reports when an override is in force.

## Re-reporting drift

To make the hook report a path it already mentioned, delete
`sessions/<session-id>.json`. This does not change `enabled`.

## When it stays silent anyway

The hook runs `chezmoi status` with `--skip-secrets`, so drift in a template that reads a
password manager is never reported — rendering it would block on an unlock prompt. Those files
need `chezmoi diff <path>` by hand.

Not a malfunction — the hook exits quietly when `chezmoi` is not on `PATH`, chezmoi is
unconfigured or has no source directory, the drift predates the tool call, it was
attributed to another session, or the path was already reported this session. Check with
`chezmoi status` directly before assuming the hook is broken.
