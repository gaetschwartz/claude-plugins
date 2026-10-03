---
name: new
description: Use when the user explicitly asks to create a new guardrails rule, e.g. "block pkill for agents", "write a rule that warns on curl | sh", "add a guardrail for X". Interviews the user, tests the rule on examples and edge cases, shows it for confirmation, then writes it. Never use it to get past a guardrails denial.
argument-hint: "[-B|--block <cmd>]... [-A|--allow <cmd>]... [-s|--scope global|project|managed] [-P|--path <file>] [-a|--action deny|warn] [-R|--retry] [-m|--modes a,b] [-i|--id <id>] [-y|--yes] [description]"
allowed-tools: Bash(guardrails status *) Bash(guardrails rule test *) Bash(guardrails rule ast *) Bash(guardrails preset list *) AskUserQuestion
---

# guardrails new

Create one rule. Before anything else read `${CLAUDE_PLUGIN_ROOT}/references/changing-config.md` (ground rules: only
what the user asked, `--as-user`, never sudo, exit codes), `${CLAUDE_PLUGIN_ROOT}/references/matching.md` (what
each matcher sees) and `${CLAUDE_PLUGIN_ROOT}/references/presentation.md` (what the CLI prints and the verbatim-paste
rule).

Arguments: $ARGUMENTS

## Arguments

Parse the text above; every long flag has a short one. Flags pre-answer the matching question below.

- `-B` / `--block <cmd>` (repeatable): a command that must be caught. `-A` / `--allow <cmd>` (repeatable): a command
  that must pass. Both count as the user's own examples (source `yours`).
- `-s` / `--scope global|project|managed`, `-P` / `--path <file>` (managed-format file; needs `-s managed`),
  `-a` / `--action deny|warn`, `-R` / `--retry` (retry `same-command`; only meaningful for deny, so with `-a warn` ignore it and say so), `-m` / `--modes a,b`,
  `-i` / `--id <id>`.
- `-y` / `--yes`: skip only the final confirmation (step 1.d). Edge-case questions are still asked.
- Any remaining free text is the description of the rule; when present, skip step 1.a.
- No arguments at all: run the interactive flow from step 1.a.

Defaults when a setting is neither given nor asked: action deny, retry same-command (only when deny), scope global,
no modes.

A rule applies to Bash commands and to Monitor commands alike (the hook matches both); a Monitor call that only
opens a `ws` URL has no command, and monitors a plugin declares itself are not covered.

## Asking

- Free text (the description, examples, message wording) is always asked in a plain chat message, never with
  AskUserQuestion.
- Choices use AskUserQuestion: 2 to 4 options per question, 1 to 4 questions per call, `header` at most 12
  characters, "Other" is added automatically, the recommended option goes first with a `(Recommended)` suffix.

## How to write the rule

- One behavior per rule: split on "and"; a different message, alternative, action or mode is a different rule.
- Narrowest matcher that separates the examples: `program`, `program` + `args`, `builtin`, `ast`, `regex` (Rust regex syntax: no backreferences or look-around).
- For a structural `ast` rule run `guardrails rule ast '<a command it must catch>'` and use the kinds it prints.
- Anchor the `pattern` on the dangerous command; put its surroundings in a relation (`inside`, `has`, `follows`,
  `precedes`).
- Test at least 3 commands that must be caught (one wrapped: `sudo X`, `bash -c 'X'`, a pipe or `$( )`) and at least 3
  that must pass (a look-alike, `man X`, `echo "X"`, a heredoc mentioning X).
- Tighten to zero mismatches, then write a `message` that names the alternative.

Pitfalls (details in the full guide):

- `trailing-holes`: end a pattern with `$$$`, not `$A` (exactly one word); patterns are anchored at the start, flags
  are order-sensitive.
- `hole-in-substitution`: a hole inside `$( )` never matches; use `has` with `kind: command_substitution`.
- `stop-by`: `inside` / `has` / `follows` / `precedes` look at the nearest level only; add `stopBy: end`.
- `args-no-pipelines`: `args` sees one command's own words, never a pipe or a substitution.
- `program-and-wrapper-words`: `program` also matches behind a wrapper by any of its words (`sudo grep pkill file` is a
  hit for `pkill`); test the look-alikes.
- `regex-in-heredocs`: `regex` fires on `echo "X"`, `man X` and heredoc bodies.

Negated context (`not` around `inside` / `follows` / `precedes`) works: every rule runs on the real tree.

Read `${CLAUDE_PLUGIN_ROOT}/references/writing-rules.md` in full whenever the rule is non-trivial (anything beyond
`program` / `args`: a relation, a regex, several behaviors, wrapper or quoting concerns) or you are unsure.

Three examples (each block is machine-checked: every `catch` command matches, every `pass` command does not).

A plain pattern. `tree` shows why it matches: the command is a `command` whose `command_name` is `pkill`; `$$$` covers
the words after it, and a command pattern is also tried behind a wrapper (`sudo pkill x`) and inside `bash -c` strings.

```rule-example
{
  "id": "no-pkill",
  "title": "kill by name",
  "rule": {"ast": {"pattern": "pkill $$$"}},
  "action": "deny",
  "catch": [
    "pkill -f vite", "pkill", "sudo pkill node", "bash -c 'echo; pkill x'", "echo $(pkill x)"
  ],
  "pass": [
    "pgrep -xl vite", "man pkill", "echo \"pkill x\"", "echo pkill", "cat <<'EOF'\npkill x\nEOF"
  ],
  "tree": "command command_name word"
}
```

A contextual relation. `stopBy: end` lets `inside` climb past the parent; the quoted and heredoc passes are data.

```rule-example
{
  "id": "pgrep-in-subst-or-pipe",
  "title": "pgrep nested in a substitution or pipeline",
  "rule": {
    "ast": {
      "pattern": "pgrep $$$",
      "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}], "stopBy": "end"}
    }
  },
  "action": "deny",
  "catch": [
    "kill $(pgrep -f vite)", "pgrep -f vite | xargs echo", "echo \"pids: $(pgrep x)\"",
    "sudo sh -c 'echo $(pgrep x)'"
  ],
  "pass": [
    "pgrep -xl vite", "sudo pgrep -xl vite", "man pgrep", "echo \"pgrep x | head\"", "echo '$(pgrep x)'",
    "cat <<'EOF'\n$(pgrep x)\nEOF"
  ],
  "tree": "command_substitution command command_name"
}
```

A negation with an exception. `not` + `has` looks inside the matched command, so `--force-with-lease` is allowed;
`git -C dir push -f` and `--force-with-lease --force` are not covered (the cookbook's flags file handles the first).

```rule-example
{
  "id": "no-force-push",
  "title": "force push except with lease",
  "rule": {
    "ast": {
      "pattern": "git push $$$",
      "has": {"regex": "^(--force|-f)$"},
      "not": {"has": {"regex": "^--force-with-lease"}}
    }
  },
  "action": "deny",
  "catch": [
    "git push --force", "git push -f origin main", "sudo git push --force", "bash -c 'git push -f'"
  ],
  "pass": [
    "git push", "git push --force-with-lease", "git push --force-with-lease origin main",
    "git push -u origin feat", "man git-push", "echo \"git push -f\"", "cat <<'EOF'\ngit push -f\nEOF"
  ],
  "tree": "command command_name word"
}
```

More examples, by shape (context, pipelines, flags, lists, wrappers): `${CLAUDE_PLUGIN_ROOT}/references/ast/index.md`.
Read only the file that matches the shape you need.

## Step 1: understand the intent (loop until the user confirms)

**1.a** Ask in chat for a description and/or examples and counter-examples: commands that must be caught and commands
that must pass. Skipped when a description was passed.

**1.b.1** Elaborate the rule. Use the narrowest matcher that separates the examples, stopping at the first that does:
`program` (one name or a list), then `program` + `args`, then `builtin`, then an `ast` rule (a `pattern`, plus
`inside` / `has` when the question is about context such as "only when nested in a substitution, pipeline or loop"),
then `regex`. Before writing an `ast` rule with relations, run `guardrails rule ast '<a command it must catch>'` and
read the node kinds it prints; do not guess kinds. Write a `message` that names the
alternative (what to do instead). Derive the `id` from the intent (`no-pkill`); ask only when it collides with an id in
`guardrails status`, and say when the colliding rule is managed. Put the user's description in the rule's `description`.
A question about what a command is piped into is out of reach for `program`/`args`: use `ast` with `inside` when the
shape is structural, `regex` for dataflow across commands (`regex` also fires inside heredocs and quoted text). The
wrappers and shells themselves (`sudo`, `bash`) are programs too (`program: sudo` matches `sudo ls`). A rule with
`program`, `args`, `builtin` or `match.ast` needs the ast-grep runtime, which guardrails installs by itself (every `guardrails` call ensures it first); if
`rule test` reports `cannot` for a command or prints a note that the engine failed, tell the user before going on
(`guardrails engine status` shows the state); no rule, `regex` included, works while the engine fails.

**1.b.2** With examples, test them: `guardrails rule test --json - 'cmd' …` with the rule on stdin (see "Passing the
rule as JSON").
If no matcher can satisfy every example, say so and ask (chat) which example to drop or rephrase. Adjust the matcher
and re-test, at most 3 rounds before asking.

**1.b.3** Elaborate more examples yourself: reasonable edge cases the description does not cover (more of them when no
examples were given): wrapped forms (`sudo`, `bash -c`, `xargs`, pipelines), look-alikes (`pgrep` vs `pkill`, `man X`,
`echo "X"`, a heredoc mentioning X), and flag variations. Test them all with `rule test`.

**1.b.4** Ask about the cases that are genuinely ambiguous, meaning the user might actually want either outcome. Clear
cases are not asked: they go in the display tagged `inferred`. One AskUserQuestion question per case, up to 4 per call,
about 6 cases in total:

- `header`: `Edge case`
- question: "Should `<cmd>` be blocked?" (or "warned on?" for a warn rule) plus one short line on why it is ambiguous
- options in the infinitive, recommended first: `Allow` and `Block` (`Warn` when the rule's action is warn), each with a
  one-phrase description
- `preview`: the current `rule test` verdict for that command

A rule has one action, so only those two outcomes exist. Fold every answer back into the matcher, re-test, and make
sure the final rule agrees with every decision. Record the command as an example with source `you chose`.

**1.b.5** Settings: one AskUserQuestion call for whatever is still unset after flags and defaults:

- Action: `Deny (Recommended)` / `Warn`
- Retry (deny only): `same-command (Recommended)` / `none`
- Scope: `Global` / `Project` / `Managed`; each option's preview shows the file it writes (for managed the platform
  path, or the `--path` file)
- Modes (multi-select, at most 4 options): `none (Recommended)` and up to two existing modes from `guardrails status`
  (more go through "Other", as does naming a new mode); a new mode leads to a follow-up question on whether agents
  may enable it

Skip this call entirely in one-liner mode (a description was given) when the user passed no settings flags: use the
defaults, unless something is ambiguous. Ask for message wording in chat only when the alternative is unknown.

**1.c** Show your understanding with the CLI's own rule card; you never build it.

1. Collect every command from 1.a to 1.b.4 as an examples list: `{"cmd": "<exact command>", "source": "yours" |
   "inferred" | "you chose", "expect": "match" | "pass"}`. `source` is where the command came from. `expect` is what
   the user wants, never what the matcher did: `match` for a command they want caught, `pass` for one they want to go
   through, and for an edge case the answer they gave.
2. Run the command below with the rule and the examples in one JSON document on stdin (a quoted heredoc, so nothing
   needs shell quoting):

       guardrails rule test --json - --intent '<the user's description as one short plain line>' --id-name <id> --scope <s> [--path <file>] <<'EOF'
       {"rule": {…}, "examples": [{"cmd": "…", "source": "yours", "expect": "match"}, …]}
       EOF

3. Paste its output VERBATIM as your message: unchanged, no paraphrase, no added prose, no fence. The CLI computed the
   rows, verdicts, `wrapped` tags, spacing and counts from real results; never write a script for them and never type a
   verdict yourself.
4. A `⚠` row or a mismatch count above 0 means the rule and the user's intent disagree: fix the rule (or ask which
   example is wrong), re-run, and paste the new output instead. A `**Note**` line is part of the output: leave it in.

**1.d** AskUserQuestion: `header` `Confirm`, question "Does this match what you want?", options `Looks good
(Recommended)` and `Change something`. `Looks good` goes to step 2. `Change something` returns to 1.a, asking in chat
for more description and/or examples, keeping earlier examples and decisions. `-y` skips this step.

## Passing the rule as JSON

Use only Bash, never a temp file: `--json -` reads stdin, so put the JSON in a heredoc with a quoted delimiter
(`<<'EOF'`) and nothing inside it needs escaping for the shell (backticks, quotes, `$(` are all literal). `rule test`
and `rule add` both accept the document `{"rule": {…}, "examples": […]}`: they read the `rule` and `rule add` ignores
the examples. No `id` key in the rule; the id goes on the command line. A literal or `@<file>` also work for `--json`.
Invalid JSON exits 2 with a message. Heredoc invocations are not pre-approved by `allowed-tools`, so expect a
permission prompt on them unless the user runs in a mode that skips prompts.

## Step 2: write and report

```
guardrails rule add <id> --json - --scope <s> [--path <file>] --as-user --reason "<the user's own words>" <<'EOF'
<the identical document that 1.c tested>
EOF
```

Resend the very document the confirmed `rule test` used, unchanged, so the stored rule is the one the user
confirmed. `rule add` and `mode declare` change configuration and are not pre-approved; the user approves each.

A new mode declared for this rule is written first with `guardrails mode declare <name> [--agent-may-enable]
[--scope <s>] [--path <file>] --as-user --reason "…"`.

On a permission failure (exit 2, not writable): say so, show the printed message and the `sudo …` command, and stop;
do not run it. After a successful write run `guardrails status --rule <id> --scope <s> [--path <file>]` and
paste its one line VERBATIM. For a managed `--path` file that is neither the platform default nor the current
`GUARDRAILS_MANAGED_PATH`, repeat the note `rule add` printed: the hook enforces it only if
`GUARDRAILS_MANAGED_PATH` points there.
