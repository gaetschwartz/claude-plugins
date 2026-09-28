---
name: push
description: Use when publishing local dotfile changes from a chezmoi source directory to its remote - "push my dotfiles", "commit and push my dotfiles", "ship my chezmoi changes", after adding or editing managed files, or when a dotfiles push is rejected as non-fast-forward and needs integrating with what the remote has.
allowed-tools: Bash(chezmoi:*)
---

# Publishing chezmoi changes

## Overview

Pushing dotfiles is rarely just `git push`. Two things go wrong: local edits to deployed
files were never captured into the source, so you push an incomplete change; and the remote
has moved on, so the push is rejected and you must integrate before retrying.

**REQUIRED: read `../../references/conflict-policy.md` before resolving anything.** It
decides what you fix silently and what you put to the user. You resolve conflicts; the user
only ever chooses between outcomes you have already worked out.

## State on this machine right now

Destination edits not yet captured into the source:
!`chezmoi status --no-pager --color=false --no-tty --skip-secrets --path-style=absolute 2>&1 | grep . || echo "(no drift - destination matches target state)"`

Uncommitted source changes:
!`chezmoi git -- status --short 2>&1 | head -30 | grep . || echo "(source working tree clean)"`

Unpushed commits, and whether add/re-add will publish on its own:
!`chezmoi git -- log --oneline '@{u}..HEAD' 2>&1 | head -20 | grep . || echo "(nothing unpushed)"`
!`chezmoi dump-config --format=json 2>/dev/null | python3 -c "import json,sys;g={k.lower():v for k,v in (json.load(sys.stdin).get('git') or {}).items()};print('autoCommit:',g.get('autocommit'),' autoPush:',g.get('autopush'))" 2>/dev/null || echo "(config unreadable)"`

If all three are empty there is nothing to push - say so instead of manufacturing a commit.

## Workflow

**1. Capture uncaptured work first.**

```bash
chezmoi status                    # destination edits not yet in the source
chezmoi git -- status --short     # source changes not yet committed
```

Destination drift means an edit exists only on this machine.
`chezmoi source-path <path>` tells you how to capture it: plain file → `chezmoi re-add`,
template → port the edit into the template (`re-add` silently no-ops), encrypted →
`re-add --re-encrypt`. Ask before capturing anything that looks machine-local or secret.

**2. Check whether autoCommit already acted.**

```bash
chezmoi cat-config | grep -A2 '\[git\]'
```

With `autoCommit`/`autoPush` on, `chezmoi add`/`edit` may already have committed and pushed.
Check `chezmoi git -- log --oneline '@{u}..HEAD'` before assuming there is anything to do.

**3. Commit what is staged-worthy.** Follow the repo's existing commit style — read
`chezmoi git -- log --oneline -10` first. Stage explicitly; a chezmoi source tree can hold
generated or machine-local files you do not want in the commit.

**4. Push.**

```bash
chezmoi git -- push
```

If it succeeds, report and stop.

**5. If rejected (non-fast-forward), integrate — do not force.**

```bash
chezmoi git -- fetch
chezmoi git -- log --oneline 'HEAD..@{u}'     # what the remote gained
chezmoi git -- diff --stat 'HEAD..@{u}'
chezmoi git -- rebase '@{u}'
```

Resolve any conflict with the conflict policy: fix the evident ones yourself, escalate only
genuine design decisions, and present them as named options with consequences. Never leave
conflict markers — `chezmoi git -- grep -n '^<<<<<<<'` before continuing. Never
`push --force` unless the user explicitly asks for it.

**6. Push again, then reconcile this machine.**

Integrating brought in changes this machine has not applied yet:

```bash
chezmoi git -- push
chezmoi status                    # R in column 2 = an incoming script will run
chezmoi diff
chezmoi apply
```

**Incoming `run_` scripts execute arbitrary code from another machine.** Read any new or
changed script before applying, and say what it does.

## Report what happened

Always finish with:

- **pushed** — the commits that went up, one line each
- **pulled** — anything that came in during integration, and what it touched
- **applied** — what changed on this machine as a result, and any scripts that ran
- **resolved** — conflicts you settled, and which way
- **left alone** — drift you deliberately did not capture, and why

If nothing needed pushing, say that plainly rather than manufacturing a commit.

## Red flags

- About to `push --force` to clear a rejection → integrate instead; the remote is shared across machines.
- About to commit without checking `chezmoi status` → you may be publishing a half-captured change.
- About to ask the user to resolve a conflict themselves → re-read the conflict policy; present options.
- Pushed after a rebase but skipped `chezmoi apply` → this machine is now behind its own source.
