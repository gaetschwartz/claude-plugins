---
name: explain
description: Use when someone asks why a command was denied or warned on, why a command was not caught, or what a guardrails rule covers, e.g. "why was my command denied?", "what does the no-pkill rule cover?", "why doesn't this rule catch curl | sh?". Callers, including other agents, must pass the rule id or the exact command, because this runs without the conversation. Denial messages remain the first source.
argument-hint: "[<id>] [-c|--command '<cmd>'] [-s|--scope global|project|managed] [-P|--path <file>]"
context: fork
model: sonnet
background: false
allowed-tools: Bash(guardrails status *) Bash(guardrails rule test *) Bash(cat ${CLAUDE_PLUGIN_ROOT}/references/*)
disallowed-tools: Edit Write NotebookEdit
---

# guardrails explain

Explain what a guardrails rule covers and why a command was or was not caught. Read-only: run only
`guardrails status` and `guardrails rule test`. Never run a command that adds, sets, removes, enables or disables
anything, and never suggest editing a rule because a command was blocked.

Arguments: $ARGUMENTS

Flags (each long flag has its short form): `<id>` the rule id; `-c` / `--command '<cmd>'` the exact command to
explain; `-s` / `--scope global|project|managed` describe only what that layer's entry of the rule contributes; `-P` / `--path <file>` an extra
managed-format file, passed through as `--path <file>` to both commands.

This skill runs in a fork with no conversation history. A caller must pass the rule id or the exact command text; "the
command that was just denied" cannot be resolved. If the arguments contain neither, do not guess: reply in one line
asking for the rule id or the exact command, and stop. (Run from the main session with no arguments, ask the user in
plain chat what to explain instead.)

## Matching semantics

!`cat ${CLAUDE_PLUGIN_ROOT}/references/matching.md`

## Presentation conventions

!`cat ${CLAUDE_PLUGIN_ROOT}/references/presentation.md`

## Steps

1. The references below are already loaded above; use them, do not guess.
2. Run `guardrails status` (with `--path` when given) to see the effective rules, origins, active modes and problems.
   A deny message names the rule as `[guardrails:<id>]`, or `<id> (managed)`.
3. With an id: run `guardrails rule test --id <id> '<cmd>' …` on the command from `-c`, and on the few commands
   that answer the question (the one in question, its wrapped and look-alike forms). With only a command: find the
   rules whose `program`/`regex` could apply and test that command against each of them by id.
4. Answer with the **Explain layout** from the presentation reference. State the cause when the matching reference
   explains it; do not hedge and do not say "possibly". The reference covers the usual causes:
   - a wrapper or `bash -c` was looked through, or was not (ssh, `find -exec`, scripts are invisible)
   - `args` only sees that one command's own arguments, so a pipe to `sh` is invisible to it and needs `match.regex`
   - the rule was suspended by an active mode, disabled, or skipped because `requires` is not installed, or the hook
     or project rules are switched off (the "Why a rule may not fire" list)
   - `program` never matches a wrapper or shell itself (`sudo`, `bash`); `bash script.sh` is program `script.sh`
   - a retry acknowledged the identical command earlier in the session
   - a project or global entry cannot loosen what a higher layer defines; a managed rule without modes is always
     enforced
5. When the fix for a pipeline case is a rule change, name `match.regex` as the fix and leave the change to the user
   (`guardrails:edit`). Only mention verified behaviour: every ✗ or ✓ you show came from `rule test` in this run, and
   the `Verified` line counts them and reads "matcher checked with `rule test`". `rule test` checks only the matcher:
   repeat its `note:` lines (disabled, missing binary, suspending mode, hook off) below `Verified`, and take mode, retry
   and warn-versus-deny behaviour from `status` and the reference.
