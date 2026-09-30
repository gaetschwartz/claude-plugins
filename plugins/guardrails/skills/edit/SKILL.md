---
name: edit
description: Use when the user explicitly asks to change, disable, enable, reword or remove an existing guardrails rule, e.g. "make the strings rule a warning", "turn off the pkill rule in this repo", "remove no-pkill". Never use it to get past a guardrails denial.
argument-hint: "<id> [enable|disable|rm|key=value ...] [-s|--scope global|project|managed] [-P|--path <file>] [-y|--yes]"
allowed-tools: Bash(guardrails status *) Bash(guardrails rule test *) Bash(guardrails rule ast *) AskUserQuestion
---

# guardrails edit

Change or remove one existing rule. Before anything else read `${CLAUDE_PLUGIN_ROOT}/references/changing-config.md`
(ground rules, sudo handling, exit codes). When a change touches `match`, also read
`${CLAUDE_PLUGIN_ROOT}/references/matching.md`, and in every case
`${CLAUDE_PLUGIN_ROOT}/references/presentation.md` (what the CLI prints and the verbatim-paste rule).

Arguments: $ARGUMENTS

## Arguments

Parse the text above; every long flag has a short one.

- `<id>`: the rule. `enable` / `disable`: set `enabled=true` / `enabled=false` on the rule (this is the rule's own flag,
  not the `guardrails enable|disable` hook verbs, which agents cannot run). `rm`: remove it. `key=value` pairs are
  passed to `guardrails rule set`; the keys are `action`, `retry`, `enabled`, `modes`, `message`, `messageShort`,
  `description`, `program`, `args`, `builtin`, `regex`, `ast`, `requires`. Comma lists for `modes`, `requires` and
  `program`; an empty value clears a field; `program`, `args`, `builtin`, `regex`, `ast` edit `match` (`ast` is a
  JSON object: pass it through `--json -`, as a JSON object value).
- `-s` / `--scope global|project|managed` (default global), `-P` / `--path <file>` (managed-format file, needs
  `-s managed`), `-y` / `--yes` (do not confirm `rm`).
- No arguments: run `guardrails status --render`, paste its output VERBATIM, ask (AskUserQuestion, or chat when there
  are more than four) which rule, then ask what to change. A rule given without a change: paste the output of
  `guardrails status --render --rule <id>` VERBATIM and ask what to change.

## Steps

1. Look the rule up with `guardrails status --render --rule <id>` (add `--path` when given) to learn its origins and
   state. A managed rule can only be changed with `--scope managed` (exit 3 otherwise); a project entry over a global
   or managed rule can only tighten it, and the CLI says which keys had no effect: tell the user.
2. Apply it:
   - changes: `guardrails rule set <id> key=value … --scope <s> [--path <file>] --as-user --reason "<user's words>"`
   - text with quotes, backticks, `$(` or several lines (a `message`, a `regex`, an `args`): pass the fields as a JSON
     object on stdin through a quoted heredoc, `guardrails rule set <id> --json - … <<'EOF'` … `EOF`; keys are the same
     as above, lists are JSON arrays, `enabled` is a boolean. Do not quote such text on the command line.
   - `rule set` and `rule rm` change configuration and are not pre-approved; heredoc invocations are never
     pre-approved either, so expect permission prompts unless the user's mode skips them.
   - removal: confirm first with AskUserQuestion (`header` `Remove`, question "Remove rule `<id>` from `<scope>`?",
     options `Remove` and `Keep (Recommended)`) unless `-y`; then `guardrails rule rm <id> --scope <s> [--path <file>]
     --as-user --reason "…"`
3. For a non-trivial change to `match`, read `${CLAUDE_PLUGIN_ROOT}/references/writing-rules.md`; worked `ast` rules
   by shape are in `${CLAUDE_PLUGIN_ROOT}/references/ast/index.md` (read only the file that matches).
   When a change to `match` uses `ast`, follow the matcher ladder (`program`, `program` + `args`, `builtin`, `ast`
   pattern with `inside` / `has`, `regex`) and run `guardrails rule ast '<command>'` to read the node kinds before
   writing relational rules. After a change to `match` (program, args, builtin, regex, ast), re-verify: write the
   commands the user gives, or sensible ones (a caught command, a wrapped form, a look-alike that must pass), as an examples list
   (`{"cmd", "source": "yours" | "inferred", "expect": "match" | "pass"}`, `expect` from what the user wants) on stdin
   and run `guardrails rule test --render --id <id> --examples - [--path <file>] <<'EOF'` … `EOF`. Paste the output VERBATIM:
   unchanged, no paraphrase, no added prose. Never write a script, and never build rows, verdicts or spacing yourself.
   A `⚠` row or a mismatch count above 0 goes back to the user, not into a silent second edit.
4. Report the outcome: after a `set`, run `guardrails status --render --rule <id> --scope <s> [--path <file>]` and paste
   its one line VERBATIM; after a `rm`, the CLI's "removed" line is the report. On exit 2 because the file is not
   writable, print the message and the `sudo …` command exactly as printed and stop; do not run it.
