---
name: status
description: Use when the user asks what guardrails rules, modes or problems are active, e.g. "what guardrails are active?", "list my guardrails rules", "is the managed file in use?", "are any guardrails rules suspended?". A plain list, no explanation; use guardrails:explain for why a rule did or did not catch a command.
argument-hint: "[-s|--scope global|project|managed] [-p|--problems] [-P|--path <file>]"
context: fork
model: haiku
background: false
allowed-tools: Bash(guardrails status *) Read(/${CLAUDE_PLUGIN_ROOT}/references/**)
---

# guardrails status

List the effective guardrails state compactly. Do not explain rules, do not advise, do not change anything.

Arguments: $ARGUMENTS

Flags (parse them from the text above; each long flag has its short form):

- `-s` / `--scope global|project|managed`: list only rules and modes that have an entry in that layer.
- `-p` / `--problems`: print only the problems section.
- `-P` / `--path <file>`: pass it through as `--path <file>` (an extra managed-format file, read like the
  `GUARDRAILS_MANAGED_PATH` override).

## Steps

1. Run `guardrails status` (add `--path <file>` when `-P` was given). Nothing else. If it fails, print the error line
   and stop.
2. Read `${CLAUDE_PLUGIN_ROOT}/references/presentation.md` and print the **Status layout** exactly as described there,
   built only from the command's output: unfenced markdown, no tables, no code fences, no prose paragraphs.
3. Map the CLI text to it:
   - header counts come from the `rules:` list; `hook on` or `hook off` from `hook enabled:`
   - `managed state:` / `managed override:` / `managed --path:` become the **Managed** line. State plainly when
     the platform file is `(absent)` and an override or `--path` file is in use; keep the CLI's `note:` lines
     about that, and about a `--path` file the hook will not enforce
   - each rule line becomes a row: padded id, action, `retry`, origins from `[…]`, state from `DISABLED`,
     `SUSPENDED by …`, `ALWAYS ENFORCED` (lower-case them); drop the match summary and the indented `suspended by modes:` lines
   - each mode line becomes a row: on or off (`ACTIVE` means on), `agent may enable: yes|no`, origins
   - `problems:` entries are copied verbatim under **Problems**; omit the heading when there are none
   - no rules at all: say `No rules installed. guardrails:setup installs presets.` as the only line under **Rules**
4. Apply `-s` by dropping rows whose origins do not include that layer; `-p` by printing only **Problems** (or
   `No problems.`).

Print nothing after the list.
