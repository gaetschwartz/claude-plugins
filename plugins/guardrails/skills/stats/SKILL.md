---
name: stats
description: Use when the user asks how often guardrails rules fired or how long they take, e.g. "how often did each guardrails rule fire?", "which rules are slow or never fire?", "show guardrails stats for no-pkill". Counts and timings only, no explanation; use guardrails:explain for why a rule did or did not catch a command. Show the result to the user unchanged.
argument-hint: "[-d|--days N] [-s|--slow] [<rule>]"
context: fork
model: haiku
background: false
allowed-tools: Bash(guardrails stats *)
---

# guardrails stats

Show the per-rule telemetry compactly. Do not explain, do not advise, do not change anything.

Arguments: $ARGUMENTS

Flags (parse them from the text above), all passed straight to the CLI:

- `-d` / `--days N` becomes `--days N`: the window, 7 days by default.
- `-s` / `--slow` becomes `--slow`: sort the rules by average time.
- a rule id becomes the positional argument: that rule's day by day counts.

## Steps

1. Run `guardrails stats` plus the flags above. Nothing else; never pass `--reset`. If it fails, print the error line and
   stop.
2. Your reply is that output VERBATIM: unchanged, no paraphrase, no reordering, no summary, no added or removed lines,
   no code fence.

Print nothing after the output. The caller shows your reply to the user as it is, so write it as the final output,
with no preamble.
