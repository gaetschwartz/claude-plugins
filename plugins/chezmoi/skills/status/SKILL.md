---
name: status
description: Use when the user asks about the state of their chezmoi dotfiles - "chezmoi status", "are my dotfiles in sync", "what's drifted", "do I have anything to push", "anything to pull", "did my dotfiles change", checking before a machine handover or reinstall, or diagnosing why an expected dotfile change has not appeared.
allowed-tools: Bash(chezmoi:*)
context: fork
model: sonnet
background: false
---

# chezmoi status

Report the state of the user's chezmoi dotfiles. Focus, if any: $ARGUMENTS

The state below was captured when this skill loaded. **Read it before running anything** —
in most cases it already answers the question and no further commands are needed.

## Destination drift

Entries where the deployed file and the target state disagree. Empty means in sync.

!`chezmoi status --no-pager --color=false --no-tty --skip-secrets --path-style=absolute 2>&1 | grep . || echo "(no drift - destination matches target state)"`

Entries whose template reads a password manager are skipped (`--skip-secrets`), so they never
appear above. That is deliberate — rendering them would block on an interactive unlock prompt.
Check one explicitly with `chezmoi diff <path>` when it matters.

## Source repo working tree

Uncommitted changes in the source directory. Empty means clean.

!`chezmoi git -- status --short 2>&1 | head -40 | grep . || echo "(source working tree clean)"`

## Branch and sync position

!`chezmoi git -- status -sb 2>&1 | head -1 || echo "(unknown)"`
!`echo "unpushed commits: $(chezmoi git -- log --oneline '@{u}..HEAD' 2>/dev/null | wc -l | tr -d ' ')   unpulled (as of last fetch): $(chezmoi git -- log --oneline 'HEAD..@{u}' 2>/dev/null | wc -l | tr -d ' ')"`

Counts come from cached refs. They are only as fresh as the last fetch — say so if it matters,
and run `chezmoi git -- fetch` before claiming the remote has nothing new.

## Configuration that changes your advice

!`chezmoi dump-config --format=json 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);g={k.lower():v for k,v in (d.get('git') or {}).items()};print('sourceDir  :',d.get('sourceDir'));print('autoCommit :',g.get('autocommit'),'  autoPush:',g.get('autopush'));print('encryption :',d.get('encryption') or 'none')" 2>/dev/null || echo "(config unreadable)"`

## Reading the drift column

`chezmoi status` prints two columns. First = the destination changed since chezmoi last
wrote it. Second = what `chezmoi apply` would do.

| Code | First column | Second column |
|---|---|---|
| space | no change | no change |
| `A` | entry was created | entry will be created |
| `D` | entry was deleted | entry will be deleted |
| `M` | entry was modified | entry will be modified |
| `R` | — | script will run |

So `MM` means someone edited the deployed file **and** applying would overwrite it — the case
that loses work. ` M` means only the source moved ahead; applying is safe.

## Answering well

Lead with the verdict — in sync, or the specific thing that is not — then the detail. Do not
re-run the commands above to restate what is already here.

For each drifted entry, the right advice depends on the source type. `chezmoi source-path
<path>` tells you which: a plain file takes `chezmoi re-add`, a `.tmpl` source silently
ignores `re-add` and must be edited at the template (`encrypted_` sources take a plain
`re-add`). If `autoCommit`/`autoPush` are true, say that capturing the edit will also
commit and push.

Report `R` entries explicitly — a pending script runs code on the next apply, and the user
should know before it happens.

Never run a mutating command (`apply`, `re-add`, `add`, `destroy`, any `git` write). Name
the command that would fix each issue; the caller decides whether to run it.

## Going further

Where relevant, point the caller at `chezmoi:update` (pull and integrate remote changes)
or `chezmoi:push` (capture, commit and publish local changes). Use `chezmoi diff <path>`
yourself when the content of a drift matters to the answer.
