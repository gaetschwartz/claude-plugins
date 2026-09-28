---
name: setup
description: Use when the user wants to set up guardrails, install recommended guardrails rules, or asks which guardrails suit the work done on this machine or in this repo, e.g. "set up guardrails", "install the recommended agent rules", "I do reverse engineering in this repo, configure guardrails for that".
allowed-tools: Bash(guardrails *) AskUserQuestion
---

# Setting up guardrails

Nothing is enforced until rules are installed. This skill interviews the user and installs presets. Every command
below changes configuration on the user's behalf, so each one carries `--as-user`.

## Current state

!`python3 "${CLAUDE_SKILL_DIR}/../../lib/guard.py" status 2>&1 || echo "(could not read guardrails state)"`

## Presets

!`python3 "${CLAUDE_SKILL_DIR}/../../lib/guard.py" preset list 2>&1`

## Steps

1. Ask (AskUserQuestion, multiSelect) what kind of work happens here: general development, reverse-engineering or
   binary analysis, infrastructure / ops, other. Then ask the scope: every project (global) or only this project
   (`--project`).
2. Suggest presets:

   | work | presets |
   |---|---|
   | general development | `docs-first`, `process-safety`, `modern-cli` (its rules only act when fd / rg are installed) |
   | reverse-engineering | `docs-first` with its `reverse-engineering` mode, which could be persistently on for this project |
   | infrastructure / ops | `process-safety` (its `incident` mode relaxes it while firefighting) |

3. For each suggested preset, run `guardrails preset show <name>` and explain each rule in one line: what it catches,
   deny or warn, whether a retry is allowed. Let the user pick rules (AskUserQuestion, multiSelect).
4. For each mode the picked rules reference, ask whether an agent may switch it on for a session when the user says
   the session is that kind of work. For a reverse-engineering project, also offer to switch the mode on for the whole
   project.
5. Install and apply the answers:

   ```bash
   guardrails preset install <name> --only <id,id> --as-user [--project] --reason "setup: <summary of answers>"
   guardrails mode declare <mode> --description "<the preset's description>" [--agent-may-enable] --as-user [--project]
   guardrails mode on <mode> --scope project --as-user   # only if they chose "always on in this project"
   ```

6. Run `status` and show the user the result.

On a re-run, compare with the current state: `preset install` reports added / replaced / unchanged, and keeps existing
mode declarations. Remove only rules the user deselects (`rule rm <id> --as-user`).
