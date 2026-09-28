---
name: update
description: Use when pulling dotfile changes into a chezmoi-managed machine - "update my dotfiles", "chezmoi update", "pull my dotfiles", "sync this machine", picking up changes made on another machine, or when a chezmoi pull, rebase, or apply reports a conflict, leaves conflict markers, or would overwrite local edits.
allowed-tools: Bash(chezmoi:*)
---

# Updating a chezmoi machine

## Overview

`chezmoi update` is `git pull --autostash --rebase` followed by `chezmoi apply`. Run as one
opaque step it can lose work in two different places, so drive the steps yourself.

**REQUIRED: read `../../references/conflict-policy.md` before resolving anything.** It
decides what you fix silently and what you put to the user. You resolve conflicts; the user
only ever chooses between outcomes you have already worked out.

## State on this machine right now

Destination drift (settle this before applying):
!`chezmoi status --no-pager --color=false --no-tty --skip-secrets --path-style=absolute 2>&1 | grep . || echo "(no drift - destination matches target state)"`

Uncommitted source changes:
!`chezmoi git -- status --short 2>&1 | head -30 | grep . || echo "(source working tree clean)"`

Sync position (cached refs - fetch in step 2 for the truth):
!`chezmoi git -- status -sb 2>&1 | head -1 || echo "(unknown)"`

## Two layers, two kinds of loss

| Layer | Conflict between | What gets lost if mishandled |
|---|---|---|
| **Source repo** (git) | This machine's commits vs the remote's | A change another machine made — affects every machine |
| **Destination** (apply) | Local edits to deployed dotfiles vs incoming target state | This machine's uncommitted edits, silently |

Destination drift is the one people forget. `apply` overwrites it without a backup, so
settle it *before* applying, not after.

## Workflow

**1. Snapshot before touching anything.**

```bash
chezmoi status                          # destination drift
chezmoi git -- status --short           # uncommitted source changes
chezmoi git -- rev-parse --short HEAD   # so you can report what moved
```

**2. Fetch, don't pull.** Look before you integrate.

```bash
chezmoi git -- fetch
chezmoi git -- log --oneline 'HEAD..@{u}'     # incoming
chezmoi git -- diff --stat 'HEAD..@{u}'       # what they touch
```

Nothing incoming and no local drift → say so and stop. Don't manufacture work.

**3. Deal with uncommitted source changes explicitly.** Don't leave them to `--autostash`;
a stash that fails to re-apply after a rebase is a confusing place to debug from. Commit
them if they are finished work, otherwise stash deliberately and note it.

**4. Settle destination drift before applying.** For each entry `chezmoi status` reports,
`chezmoi source-path` tells you whether it is a plain file
(`re-add`), a template (`re-add` silently no-ops — port the edit into the template), or
unmanaged. Resolve per the conflict policy.

**5. Integrate.**

```bash
chezmoi git -- rebase '@{u}'
```

On conflict: read both sides, classify with the conflict policy, resolve in the working
tree, `chezmoi git -- add <path>`, `chezmoi git -- rebase --continue`. Never leave markers —
`chezmoi git -- grep -n '^<<<<<<<'` before continuing.

**6. Check what apply would do — especially scripts.**

```bash
chezmoi status                    # R in column 2 = a script will run
chezmoi diff                      # includes script contents by default
```

**Incoming `run_` scripts execute arbitrary code from another machine.** If step 2 showed a
new or changed script, read it before applying and say what it does in your summary. A
changed `run_once_` script is an escalation per the policy.

**7. Apply, then verify.**

```bash
chezmoi apply
chezmoi status                    # empty = converged
```

If the config template itself changed, use `chezmoi update --init` (or `apply --init`) so
the config is regenerated. `chezmoi doctor` if anything looks off.

## Report what happened

Always finish with:

- commits pulled in (`HEAD` before → after), and what they changed
- destination edits you captured, and how (`re-add`, into a template, ignored)
- conflicts you resolved, and which way — one line each
- scripts that ran
- anything left for the user

Say plainly if you stashed something, or if a local edit was deliberately discarded.

## Red flags

- About to run `chezmoi apply --force` to clear a conflict → that destroys the local edit. Resolve it instead.
- About to run `chezmoi merge` or a `vimdiff`-based merge tool in a non-interactive session → it hangs. Resolve in the working tree.
- About to ask the user to fix a conflict themselves → re-read the conflict policy; present options instead.
- `chezmoi apply` before settling destination drift → you are about to overwrite it silently.
