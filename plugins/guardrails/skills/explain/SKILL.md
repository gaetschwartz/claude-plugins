---
name: explain
description: Use when someone asks why a command was denied or warned on, why a command was not caught, or what a guardrails rule covers, e.g. "why was my command denied?", "what does the no-pkill rule cover?", "why doesn't this rule catch curl | sh?". Callers, including other agents, must pass the rule id or the exact command, because this runs without the conversation. Denial messages remain the first source.
argument-hint: "[<id>] [-c|--command '<cmd>'] [-s|--scope global|project|managed] [-P|--path <file>]"
context: fork
model: sonnet
background: false
allowed-tools: Bash(guardrails status *) Bash(guardrails rule test *) Bash(guardrails rule ast *) Bash(cat ${CLAUDE_PLUGIN_ROOT}/references/*)
disallowed-tools: Edit Write NotebookEdit
---

# guardrails explain

Explain what a guardrails rule covers and why a command was or was not caught. Read-only: run only
`guardrails status`, `guardrails rule test` and `guardrails rule ast`. Never run a command that adds, sets, removes, enables or disables
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

1. The references above are already loaded; use them, do not guess.
2. Run `guardrails status` (with `--path` when given) to see the effective rules, origins, active modes and problems.
   A deny message names the rule as `[guardrails:<id>]`, or `<id> (managed)`.
3. Decide the commands that answer the question: the one in question (from `-c`; source `yours`), its wrapped and
   look-alike forms (source `inferred`). Set `expect` only when the caller said what should happen. With an id run
   `guardrails rule test --id <id> --examples - [--path <file>]` with the examples list on stdin:

       guardrails rule test --id no-pkill --examples - <<'EOF'
       [{"cmd": "sudo pkill -f vite", "source": "yours"}, {"cmd": "pgrep -fl node"}]
       EOF

   A heredoc invocation is not pre-approved by `allowed-tools`, so it may prompt; that is expected.

   With only a command, find the rules whose `program` or `regex` could apply and run it once per rule id.
4. Answer with the rule card: paste the `rule test` output VERBATIM, unchanged, no paraphrase, no added prose inside
   it. Never write a script or compute rows, spacing, verdicts or counts yourself; every ✗ or ✓ comes from the CLI.
5. After a blank line add the bold-label lines from the Explain section of the presentation reference (`Happens`,
   `Loosen`, `Lower layers`, and `Cause` only when the question was why something was or was not caught), with no
   command spans or verdicts of your own. State the cause when the matching reference explains it; do not hedge and do
   not say "possibly". The reference covers the usual causes:
   - a wrapper or `bash -c` was looked through, or was not (ssh, `find -exec`, scripts and obfuscated or dynamic names are invisible)
   - `args` only sees that one command's own arguments, so a pipe to `sh` is invisible to it and needs `match.ast`
     (an `inside` relation) or `match.regex`
   - for a rule with `match.ast`: run `guardrails rule ast '<cmd>'` to show the tree and the shell-string units; the
     engine may have been unavailable (the rule is then not enforced and the row is listed under **Not evaluated**; the user can run `guardrails engine status` for the fix), a wrapper may be unknown to the wrapper list, or the
     text was data (a heredoc body, single quotes)
   - the rule was suspended by an active mode, disabled, or skipped because `requires` is not installed, or the hook
     or project rules are switched off (the "Why a rule may not fire" list)
   - `program` matches a wrapper or shell by name and behind a wrapper by any of its words (`sudo grep pkill file` hits
     `pkill`); `bash script.sh` is program `bash`, the script file is not looked into
   - a retry acknowledged the identical command earlier in the session
   - the call was not a command the hook sees: a Monitor with only a `ws` URL, or a monitor a plugin declares itself
   - a project or global entry cannot loosen what a higher layer defines; a managed rule without modes is always
     enforced
6. When the fix for a pipeline case is a rule change, name `match.ast` (or `match.regex`) as the fix and leave the change to the user
   (`guardrails:edit`). The card's `Verified` line and `Note` come from `rule test`, which checks only the matcher:
   take mode, retry and warn-versus-deny behaviour from `status` and the reference.
7. To explain why a construct was or was not caught, or what a fix would look like, `cat` the guide
   `${CLAUDE_PLUGIN_ROOT}/references/writing-rules.md` and the one matching cookbook file listed in
   `${CLAUDE_PLUGIN_ROOT}/references/ast/index.md`; they are not loaded above.
