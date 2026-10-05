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
   scan scope (`files_scanned`, `files_total`, `stopped_early`, `skipped_other_version`, `skipped_unhashed`,
   `dropped_unknown_rules`, `corrupt_lines`, `unreadable_files`).
2. The CLI already keeps only denials from the CURRENT version of the rule (matched by the hash in the denial) and counts
   what it skipped. So judge each hit against the rule as it is today; never replay anything yourself, never judge an
   older version, and report the skipped counts. Right after a rule edit the audit can legitimately show no hits, because
   the history of older versions is skipped; say that instead of treating it as a failure.
3. With no hits, say so in one plain line with the scope (files scanned of total, the rule if one was given, the skipped
   counts) and stop.
4. Judge each hit from the evidence in the JSON only: the denied `command`, `matched` (the statement of the command the
   current rule matches; null with a `matched_note` when it could not be reproduced), the denial `message` (it states
   what the rule is for), `cwd`, and the `before` and `after` messages. Do not read files, run the command, or guess
   beyond what the messages show. For each hit:
   a. Read the assistant text before the call. If the agent was deliberately probing the guard (checking that a denial
      happens, with obviously fake targets), the verdict is `test`, not a false positive.
   b. Otherwise decide `warranted` or `false-positive`, or `unclear` when the evidence does not settle it. Judge the
      `matched` statement against what the denial message says the rule forbids; read the rest of the command only for
      context (a long script is never judged as a whole). When `matched` is null, locate the statement the denial message
      describes and judge that one. Note harmless forms that are forbidden by design (for example `pkill -0` existence
      checks): they are still `warranted`.
   c. Record what the agent did next, from the `after` messages: `adapted` (took the rule's advice or an equivalent safe
      route), `worked-around` (reached the same effect another way that dodges the rule), `gave-up`, `asked-user`, or
      `unknown`.
   d. Say whether it came from the main session or a subagent (`is_sidechain`).
   e. Give `confidence` (`low`, `medium`, `high`) and a reason of one or two sentences that cites the evidence (quote a
      few words of the statement or a message).
   f. For a `false-positive`, give a concrete suggested narrowing: in plain words what the rule should not match, plus the
      rule id. A suggestion only; never edit anything. A `worked-around` response on a `warranted` hit is worth a short
      note: the rule may be incomplete.
5. Reply in this order, with no preamble:
   - a summary table with one row per rule and separate counts for `warranted`, `false-positive`, `test` and `unclear`;
     then the false-positive rate over the non-test hits only (`test` hits never count toward it), then a line with the
     scan scope and every non-zero skipped, dropped, corrupt or unreadable count in the CLI's own words (other version of
     the rule, recorded before rule hashing, rule no longer exists).
   - one short entry per hit, newest first: local time, rule and tool, main session or subagent, the matched statement
     (or the command) cut to about 120 characters, then the verdict, confidence, agent response and the reason.
   - for any false positive, the suggested narrowing and that the user can apply it with `guardrails:edit` (or
     `guardrails:new` for a new rule). Do not run either yourself.
6. Redact anything in what you show that looks like a token, password or key (a long random string, `Bearer ...`,
   `token=...`, a URL with credentials) as `[redacted]`. No transcript dumps: quote a few words at most.

The caller shows your reply to the user as it is, so write it as the final output.
