---
name: status
description: Use when the user asks what guardrails rules, modes or problems are active, e.g. "what guardrails are active?", "list my guardrails rules", "is the managed file in use?", "are any guardrails rules suspended?". A plain list, no explanation; use guardrails:explain for why a rule did or did not catch a command. Show the result to the user unchanged.
argument-hint: "[-s|--scope global|project|managed] [-p|--problems]"
context: fork
model: haiku
background: false
allowed-tools: Bash(guardrails status *)
---

# guardrails status

List the effective guardrails state compactly. Do not explain rules, do not advise, do not change anything.

Arguments: $ARGUMENTS

Flags (parse them from the text above; each long flag has its short form), all passed straight to the CLI:

- `-s` / `--scope global|project|managed` becomes `--scope <layer>`: only rules and modes with an entry in that layer.
- `-p` / `--problems` becomes `--problems`: only the problems.

## Steps

1. Run `guardrails status` plus the flags above. Nothing else. If it fails, print the error line and stop.
2. Your reply is that output VERBATIM: unchanged, no paraphrase, no reordering, no summary, no added or removed lines,
   no code fence. The CLI already computed the managed-file line, the rows, the states and the problems. Never
   rebuild, filter or reformat them yourself.

Print nothing after the output. The caller shows your reply to the user as it is, so write it as the final output,
with no preamble.
