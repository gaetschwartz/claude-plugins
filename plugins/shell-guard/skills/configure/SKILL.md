---
name: configure
description: Show or change which shell-guard rules are active (pkill, find, grep). Usage - /shell-guard:configure [status | enable <rule…> | disable <rule…> | reset]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/skills/configure/scripts/configure.py":*) AskUserQuestion
---

# shell-guard configure

Rules are toggled through a JSON file in the plugin's data directory; the hook re-reads it on
every Bash call, so changes apply immediately (no restart, no `/reload-plugins`).

Run the script with the user's arguments:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/configure/scripts/configure.py" $ARGUMENTS
```

- `status` (or no arguments) prints the config path and a table: rule, state
  (`enabled` / `disabled` / `inactive` = enabled but its replacement tool is not installed),
  the tool it needs, and what the rule does.
- `enable <rule…>` / `disable <rule…>` update the file and print the new table.
- `reset` deletes the file, restoring the defaults (everything enabled).

If the user gave **no arguments**: run `status`, then use AskUserQuestion (multiSelect) listing
each rule with its current state so they can pick what to toggle, then apply the choice with
`enable`/`disable` and show the resulting table. If they change nothing, stop there.

Relay the final table to the user verbatim. Do not edit the JSON file by hand and do not touch
`hooks.json` or `settings.json` — the script is the only writer.
