---
name: setup
description: Use when the user wants to set up guardrails, install recommended guardrails presets, or asks which guardrails suit the work done on this machine or in this repo, e.g. "set up guardrails", "install the process-safety preset", "I do reverse engineering in this repo, configure guardrails for that". Never use it to get past a guardrails denial.
argument-hint: "[<preset> ...] [-s|--scope global|project|managed] [-y|--yes]"
allowed-tools: Bash(guardrails status *) Bash(guardrails preset *) Bash(guardrails mode *) Bash(guardrails rule rm *) AskUserQuestion
---

# Setting up guardrails

Nothing is enforced until rules are installed. Every command below changes configuration on the user's behalf, so each
one carries `--as-user`. Before anything else read `${CLAUDE_PLUGIN_ROOT}/references/changing-config.md`.

Arguments: $ARGUMENTS

## Current state

Running `guardrails status` below installs the matcher's runtime first when it is missing. If its output says the runtime
could not be installed, state the precise reason and stop: do not install presets or ask the interview questions while
rules cannot be enforced. The user never has to run an install command; a later session retries by itself.

!`guardrails status 2>&1 || echo "(could not read guardrails state)"`

## Presets

!`guardrails preset list 2>&1`

## Arguments

Parse the text above; every long flag has a short one: preset names (install directly, no interview),
`-s` / `--scope global|project|managed` (default global), `-y` / `--yes` (skip the final summary question).

With preset names: run `guardrails preset install <name> --as-user --scope <s> --reason "setup:
<preset>"` for each, then go to step 6. The preset's own mode declarations are kept as they are; offer the step 4
questions only when the user did not pass `-y`.

## Interview (no preset names)

1. Ask (AskUserQuestion, multiSelect) what kind of work happens here: general development, reverse-engineering or
   binary analysis, infrastructure / ops, other. Then the scope: every project (global) or only this project
   (`--scope project`). Managed (machine-wide, needs root) is offered only when the user asks for it or passed `-s managed`.
2. Suggest presets:

   - general development: `docs-first`, `process-safety`, `modern-cli` (its rules only act when fd / rg are installed)
   - reverse-engineering: `docs-first` with its `reverse-engineering` mode, which could be persistently on for this project
   - infrastructure / ops: `process-safety` (its `incident` mode relaxes it while firefighting)

3. For each suggested preset, run `guardrails preset show <name>` and explain each rule in one line: what it catches,
   deny or warn, whether a retry is allowed. Let the user pick rules (AskUserQuestion, multiSelect).
4. For each mode the picked rules reference, ask whether an agent may switch it on for a session when the user says the
   session is that kind of work. For a reverse-engineering project, also offer to switch the mode on for the whole project.
5. Install and apply the answers:

   ```
   guardrails preset install <name> --only <id,id> --as-user --scope <s> --reason "setup: <summary of answers>"
   guardrails mode declare <mode> --description "<the preset's description>" [--agent-may-enable] --as-user --scope <s>
   guardrails mode on <mode> --scope project --as-user   # only if they chose "always on in this project"
   ```

6. Run `guardrails status` and show the result as rule rows in the style of
   `${CLAUDE_PLUGIN_ROOT}/references/presentation.md`.

On a re-run, compare with the current state: `preset install` reports added / replaced / unchanged, and keeps existing
mode declarations. Remove only rules the user deselects (`guardrails rule rm <id> --as-user`).
On a not-writable scope the CLI prints a message and a `sudo …` command: show both and stop.
