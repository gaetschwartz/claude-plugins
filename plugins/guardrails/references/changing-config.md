# Rules for skills that change guardrails configuration

- Only change what the user asked for in this conversation. A guardrails denial is never a reason to edit, disable or
  remove a rule or to switch a mode on: follow the denial's message, re-run the exact command if it says so, or ask.
- Pass `--as-user` on every change and put the user's own words in `--reason "…"`.
- Pass a rule with `--json @<file>` (or `--json -` for stdin) rather than quoting it inline; `rule set` takes the same
  flag for a JSON object of fields. A missing file or invalid JSON exits 2.
- Never run `sudo`. When a write fails because the file or its directory is not writable, the CLI exits 2 and prints
  the message plus a ready-made `sudo …/bin/guardrails …` command. Show both to the user and stop; the user runs it
  themselves (for example `! sudo …` in the prompt). Do the same for every scope: try the write, and report a
  permission failure with the printed message and sudo command instead of assuming what the user may write.
- `--scope global|project|managed` picks the file; global is the default. `--scope managed` writes the platform managed
  file (or `GUARDRAILS_MANAGED_PATH`), and `--path <file>` (only with `--scope managed`) writes that managed-format
  file instead. The hook enforces a `--path` file only if `GUARDRAILS_MANAGED_PATH` points at it; the CLI prints that
  note and you repeat it to the user.
- A project entry for a global or managed rule can only tighten it (`rule add` writes it with a note saying so), and
  `rule set` / `rule rm` on a managed rule from another scope are refused (exit 3 pointing at `--scope managed`). See `${CLAUDE_PLUGIN_ROOT}/references/matching.md` for the layering.
- `guardrails enable` / `guardrails disable` (hook on/off) are refused for agents. The user runs them in a terminal.
- Exit codes: 0 ok, 2 invalid input (read the error and fix the input; a not-writable file is also 2 and prints the
  sudo command), 3 refused (relay the refusal to the user, do not work around it).
- Modes: an agent may switch on a session mode only when it is declared with agentMayEnable and only after the user
  said, in this conversation, that the session is that kind of work; quote them in `--reason`. Persistent modes
  (`--scope project|global|managed`) are configuration changes and need the user's explicit request.
