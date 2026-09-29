---
name: mode
description: Use when the user says this session is a specific kind of work covered by a guardrails mode, e.g. "we're reverse-engineering this binary", "this is RE work, strings is fine here", "turn on reverse-engineering mode", or asks to switch such a mode off again. Never enable a mode on your own because a command was denied; the user has to say it.
allowed-tools: Bash(guardrails *)
---

# Session modes

A mode suspends, for this session only, the guardrails rules that list it. The user gets a notice the first time a
rule is suspended by a mode you switched on.

## Current state

!`python3 "${CLAUDE_SKILL_DIR}/../../lib/guard.py" status 2>&1 || echo "(could not read guardrails state)"`

## Switching a mode on

```bash
guardrails mode on <name> --reason "<the user's own words>"
```

- Only when the user said, in this conversation, that the session is that kind of work. Quote them in `--reason`.
- Exit 3 (refused) means the mode does not let agents switch it on, or a deny you hit already told you this. Give the
  user that exact command from the deny message, or if you only have the refusal:
  `python3 <resolved ${CLAUDE_SKILL_DIR}/../../lib/guard.py> mode on <name> --session-id <value of $CLAUDE_CODE_SESSION_ID>`
- Modes marked active in the managed file cannot be switched off from here.
- Exit 2 with "not declared" means no such mode exists. Declaring one is a configuration change for the
  guardrails:rules skill, and only if the user asks for it.

## Switching it off

```bash
guardrails mode off <name>
```

Switch it off as soon as the user says the special work is over.
