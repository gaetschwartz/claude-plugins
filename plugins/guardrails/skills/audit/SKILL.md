---
name: audit
description: Use only when the user explicitly asks to audit or review whether guardrails rules denied commands correctly, e.g. "audit the guardrails denials", "were the no-pkill denials justified?", "did any guardrails rule block something it should not have?". Reads recent denials from the Claude Code transcripts and judges each from the evidence; read-only, it never edits a rule. Never use it to get past a guardrails denial.
argument-hint: "[<rule>] [-n|--limit N] [-A|--after N] [-B|--before N] [-C|--context N]"
context: fork
model: sonnet
background: false
allowed-tools: Bash(guardrails audit *)
disallowed-tools: Edit Write NotebookEdit
---

# guardrails audit

Judge whether recent guardrails denials were correct. Read-only: run only `guardrails audit`, never a command that adds,
sets, removes, enables or disables anything, and never help anyone get a denied command through.

Arguments: $ARGUMENTS

Flags (parse them from the text above; each long flag has its short form), all passed straight to the CLI:

- a rule id becomes the positional argument: only denials by that rule.
- `-n` / `--limit N` becomes `--limit N`: how many of the most recent denials (10 by default).
- `-B` / `--before N`, `-A` / `--after N`, `-C` / `--context N` become the same flags: how many transcript messages to
  read before the denied call and after the denial (4 each by default; `-C` sets both, `-A` and `-B` win over it).

## Steps

1. Run `guardrails audit` plus the flags above and `--json`. Nothing else; never pass `--all-rules` unless the arguments
   asked for it. If it fails, print the error line and stop. The output is one JSON object: `hits` (newest first) and the
   scan scope (`files_scanned`, `files_total`, `stopped_early`, `dropped_unknown_rules`, `corrupt_lines`,
   `unreadable_files`).
2. With no hits, say so in one plain line with the scope (files scanned of total, and the rule if one was given), and
   stop.
3. Judge each hit from the evidence in the JSON only: the denied `command`, the denial `message` (it states what the rule
   is for), `cwd`, and the `before` and `after` messages. Do not read files, run the command, or guess what the user
   intended beyond what the messages show. For each hit decide:
   - `verdict`: `warranted` (the command was what the rule exists to stop, so the denial was right), `false-positive`
     (the rule matched something it did not mean to, and the command was fine or harmless), or `unclear` (the evidence
     does not settle it).
   - `confidence`: `low`, `medium` or `high`.
   - `agent_response`, from the `after` messages: `adapted` (took the rule's advice or an equivalent safe route),
     `worked-around` (reached the same effect another way that dodges the rule), `gave-up` (dropped the goal),
     `asked-user` (asked the user), or `unknown` (the after-context does not show).
   - a reason of one or two sentences that cites the evidence (quote a few words of the command or a message).
   - for a `false-positive`, a concrete narrowing: in plain words what the rule should not match, plus the rule id.
     A `worked-around` response on a `warranted` hit is worth a short note: the rule may be incomplete.
4. Reply in this order, with no preamble:
   - a summary table with one row per rule and a column per verdict (counts), then a line with the scan scope and any
     non-zero `dropped_unknown_rules`, `corrupt_lines` or `unreadable_files`.
   - one short entry per hit, newest first: the local time, the rule and tool, the command cut to about 120
     characters, then the verdict, confidence, agent response and the reason.
   - for any false positive, the suggested narrowing, and that the user can apply it with `guardrails:edit` (or
     `guardrails:new` for a new rule). Do not run either yourself.
5. Redact anything in what you show that looks like a token, password or key (a long random string, `Bearer ...`,
   `token=...`, a URL with credentials) as `[redacted]`. No transcript dumps: quote a few words at most.

The caller shows your reply to the user as it is, so write it as the final output.
