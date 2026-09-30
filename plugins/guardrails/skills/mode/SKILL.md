---
name: mode
description: Use when the user says this session is a specific kind of work covered by a guardrails mode, e.g. "we're reverse-engineering this binary", "this is RE work, strings is fine here", "turn on reverse-engineering mode", or asks to switch such a mode on or off, or to declare or undeclare one. Never enable a mode on your own because a command was denied; the user has to say it.
argument-hint: "[on|off|declare|undeclare] [<name>] [-s|--scope session|global|project|managed] [-P|--path <file>] [-e|--agent-may-enable]"
allowed-tools: Bash(guardrails status *) Bash(guardrails mode *) Read(/${CLAUDE_PLUGIN_ROOT}/references/**) AskUserQuestion
---

# guardrails mode

A mode suspends, for this session only (or persistently when switched on in a scope), the rules that list it. The user
gets a notice the first time a rule is suspended by a mode an agent switched on. Before anything else read
`${CLAUDE_PLUGIN_ROOT}/references/changing-config.md`.

Arguments: $ARGUMENTS

## Current state

!`guardrails status 2>&1 || echo "(could not read guardrails state)"`

## Arguments

Parse the text above; every long flag has a short one: `-s` / `--scope` (session is the default for `on` / `off`;
global is the default for `declare` / `undeclare`), `-P` / `--path <file>` (managed-format file, needs `-s managed` for
anything that writes), `-e` / `--agent-may-enable` (for `declare`: agents may switch the mode on for a session).

No arguments: list the modes from the state above (name, on or off, agent may enable, origins) in the status row
style, and ask what to do.

## on

`guardrails mode on <name> --reason "<the user's own words>"` (add `--scope project|global|managed [--path <file>]
--as-user` only when the user asked for a persistent mode).

- Only when the user said, in this conversation, that the session is that kind of work. Quote them in `--reason`. Agents
  may switch on only session modes that declare `agentMayEnable`.
- Exit 3 means the mode does not let agents switch it on, or a deny you hit already told you this. Give the user the
  exact command from the deny message, or if you only have the refusal:
  `python3 <resolved ${CLAUDE_PLUGIN_ROOT}/lib/guard.py> mode on <name> --session-id <value of $CLAUDE_CODE_SESSION_ID>`
- Modes marked active in the managed file cannot be switched off from another scope, and a project cannot switch on a
  mode the managed file declares.
- Exit 2 with "not declared": no such mode exists. Offer `declare`, and only do it if the user agrees.

## off

`guardrails mode off <name>` (same scope options). Switch it off as soon as the user says the special work is over.

## declare / undeclare

Configuration changes; only when the user asks for them:
`guardrails mode declare <name> --description "<what the work is>" [--agent-may-enable] [--scope …] [--path <file>]
--as-user --reason "<user's words>"` and `guardrails mode undeclare <name> [--scope …] [--path <file>] --as-user
--reason "…"`. Ask (AskUserQuestion, `header` `Agents`, options `No (Recommended)` / `Yes`) whether agents may enable
the mode when `-e` was not passed and the user did not say. A not-writable scope prints a message and a `sudo …`
command: show both and stop.
