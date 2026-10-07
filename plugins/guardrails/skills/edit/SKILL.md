---
name: edit
description: Use when the user explicitly asks to change, disable, enable, reword or remove an existing guardrails rule, e.g. "make the strings rule a warning", "turn off the pkill rule in this repo", "remove no-pkill". Never use it to get past a guardrails denial.
argument-hint: "<id> [enable|disable|rm|key=value ...] [-s|--scope global|project|managed] [-y|--yes]"
allowed-tools: Bash(guardrails status *) Bash(guardrails rule test *) Bash(guardrails rule ast *) AskUserQuestion
---

# guardrails edit

Change or remove one existing rule. Before anything else read `${CLAUDE_PLUGIN_ROOT}/references/changing-config.md`
(ground rules, sudo handling, exit codes). When a change touches `match`, `when`, `messages` or the placeholders in a
text (`{found}`, `{ARG}`, `{{` `}}`), also read
`${CLAUDE_PLUGIN_ROOT}/references/matching.md`, and in every case
`${CLAUDE_PLUGIN_ROOT}/references/presentation.md` (what the CLI prints and the verbatim-paste rule).

Arguments: $ARGUMENTS

## Arguments

Parse the text above; every long flag has a short one.

- `<id>`: the rule. `enable` / `disable`: set `enabled` to `true` / `false` on the rule (this is the rule's own flag,
  not the `guardrails enable|disable` hook verbs, which agents cannot run). `rm`: remove it. `key=value` pairs are
  turned into a JSON object for `guardrails rule set --json`; the keys are `action`, `retry`, `enabled`, `modes`,
  `message`, `messageShort`, `description`, `match`, `wrappers`, `when`, `messages`. A comma list for `modes` becomes a
  JSON array, `enabled` and `wrappers` are booleans, `match` and `when` are JSON objects and `messages` a JSON list
  that replace the whole field (the rule's current value from its config file is the starting point), an empty value
  becomes `null`, which clears the field. A rule that still has the removed `requires` is invalid: replace it with
  `when` (`requires=fd,fdfind` is `when={"bin": ["fd", "fdfind"]}`) and clear `requires` with `requires=` in the same
  change.
- `-s` / `--scope global|project|managed` (default global), `-y` / `--yes` (do not confirm `rm`).
- No arguments: run `guardrails status`, paste its output VERBATIM, ask (AskUserQuestion, or chat when there
  are more than four) which rule, then ask what to change. A rule given without a change: paste the output of
  `guardrails status --rule <id>` VERBATIM and ask what to change.

## Steps

1. Look the rule up with `guardrails status --rule <id>` to learn its origins and
   state. A managed rule can only be changed with `--scope managed` (exit 3 otherwise); a project entry over a global
   or managed rule can only tighten it, and the CLI says which keys had no effect: tell the user.
2. Apply it:
   - changes: pass the fields as a JSON object on stdin through a quoted heredoc,
     `guardrails rule set <id> --json - --scope <s> --as-user --reason "<user's words>" <<'EOF'` … `EOF`
     (lists are JSON arrays, `enabled` is a boolean, `null` clears a field). Do not quote text with quotes, backticks
     or `$(` on the command line.
   - `rule set` and `rule rm` change configuration and are not pre-approved; heredoc invocations are never
     pre-approved either, so expect permission prompts unless the user's mode skips them.
   - removal: confirm first with AskUserQuestion (`header` `Remove`, question "Remove rule `<id>` from `<scope>`?",
     options `Remove` and `Keep (Recommended)`) unless `-y`; then `guardrails rule rm <id> --scope <s>
     --as-user --reason "…"`
3. A `match` may refer to config `matchers` (`{"matcher": "name"}`): changing one changes every rule that uses it;
   `guardrails status` lists them. For a non-trivial change to `match`, read `${CLAUDE_PLUGIN_ROOT}/references/writing-rules.md`; worked rules
   by shape are in `${CLAUDE_PLUGIN_ROOT}/references/ast/index.md` (read only the file that matches).
   Follow the matcher ladder (a `command` atom, a `command` with `args`, a `pattern` with `inside` / `has`, a
   whole-text `{"kind": "program", "regex": ...}`) and run `guardrails rule ast '<command>'` to read the node kinds
   before writing relational rules. After a change to `match`, `wrappers`, `when` or `messages`, re-verify: write the
   commands the user gives, or sensible ones (a caught command, a wrapped form, a look-alike that must pass), as an examples list
   (`{"cmd", "source": "yours" | "inferred", "expect": "match" | "pass"}`, `expect` from what the user wants) on stdin
   and run `guardrails rule test --id <id> --examples - <<'EOF'` … `EOF`. Paste the output VERBATIM:
   unchanged, no paraphrase, no added prose. Never write a script, and never build rows, verdicts or spacing yourself.
   A `⚠` row or a mismatch count above 0 goes back to the user, not into a silent second edit; so does a caught row
   whose ` · case N` / ` · default message` tag is not the text the user meant for that command.
4. Report the outcome: after a `set`, run `guardrails status --rule <id> --scope <s>` and paste
   its one line VERBATIM; after a `rm`, the CLI's "removed" line is the report. On exit 2 because the file is not
   writable, print the message and the `sudo …` command exactly as printed and stop; do not run it.
