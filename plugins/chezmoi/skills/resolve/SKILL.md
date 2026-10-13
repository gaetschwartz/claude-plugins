---
name: resolve
description: Use when the user wants their chezmoi dotfiles brought back to a clean state - "fix my dotfiles", "resolve the drift", "resolve chezmoi conflicts", "make chezmoi clean", "sync everything up", "clean up my dotfiles status" - or after chezmoi:status reports drift, a stale config, pending scripts or unpushed or unpulled commits. Fixes everything itself, asks only when it is genuinely unclear which side to adopt, and never asks the user to run a chezmoi command. Pass `push` to publish without the final yes/no.
allowed-tools: Bash(chezmoi:*), Bash(${CLAUDE_SKILL_DIR}/facts.sh:*), Bash(${CLAUDE_SKILL_DIR}/base.sh:*), Bash(${CLAUDE_SKILL_DIR}/backup.sh:*), Read, Edit, Write, AskUserQuestion
---

# Resolving a chezmoi machine

Bring this machine to `✅ in sync`: deployed files match the source, the config matches its
template, scripts have run, the source is committed and level with its remote. You run every
command yourself. The user never types a chezmoi command, and is asked only when two sides
have no defensible winner. Arguments: $ARGUMENTS

**REQUIRED: read `../../references/drift-policy.md` and `../../references/conflict-policy.md`
before deciding anything.** The first says what to do with each kind of drift, the second what
to do with source-repo conflicts, and both say what is a question.

## Facts

!`${CLAUDE_SKILL_DIR}/facts.sh`

The first part is the same facts as `chezmoi:status`: one `### FILE` block per drifted file
(status, flags, `apply_diff`, template source, `reinit_diff`), the `### CONFIG` block, `### SCRIPT`
blocks with their bodies, `### REPO`. `### RESOLVE-FACTS` adds, per file, its `class`, the
recent source history and, when the last-written content could be recovered, a three-way merge
preview. `-` is the disk and `+` is the source in `apply_diff`. A single line `✅ in sync`
means there is nothing to do.

## Workflow

1. **Nothing to do?** If the facts are `✅ in sync`, reply with that line and stop.
2. **Fetch.** `chezmoi git -- fetch`, then read what is incoming: the `⬇` counts above are
   only as fresh as the last fetch.
3. **Decide every aspect** with the drift policy. Gather before deciding: read each script
   body, the incoming diff (`chezmoi git -- diff 'HEAD..@{u}'`), `base.sh <path>` when you
   need the last-written text. Sort the results into *do it* and *ask*. Do not print this plan.
4. **Ask once.** One `AskUserQuestion` call with every genuine decision, most consequential
   first (up to four questions). Shape each as the conflict policy says: quote the two values,
   name the options with their consequences on this machine and on others, recommend one.
   Add the publish yes/no as the last question when there is room; otherwise ask it right
   after. If `push` is in the arguments, do not ask it. If you cannot ask (no way to
   prompt), leave those items untouched and list them as ⏭️ in the report.
5. **Back up.** `${CLAUDE_SKILL_DIR}/backup.sh <every deployed path apply will overwrite>
   [config file]`. Keep the directory it prints.
6. **Execute, in this order**, re-reading the facts' diffs rather than trusting memory:
   1. Capture disk edits into the source: `chezmoi re-add <path>` for plain files; write the
      merged text into the source file for a clean merge; for templates edit the template or
      the local `[data]` key as the drift policy says, then check `chezmoi cat <path>` equals
      the disk file.
   2. Commit the source. Stage explicitly. Match the repo's message style
      (`chezmoi git -- log --oneline -10`); write the message from `chezmoi git -- diff`, never
      from `chezmoi diff`, which reads the other way. Check for conflict markers first.
   3. Integrate the remote: `chezmoi git -- rebase '@{u}'`; resolve conflicts as
      `conflict-policy.md` says, `git add`, `rebase --continue`.
   4. Apply. Files you decided to overwrite or restore, with the backup already taken:
      `chezmoi apply --force <path>...`, naming exactly those paths; a plain `apply` stops at a
      prompt for any file that changed on disk and there is no terminal to answer it. Then
      `chezmoi apply --exclude=scripts` for everything else. Scripts run by naming their own
      target path: `chezmoi apply <script target>` (the script's name under `$HOME`, as the
      status shows it). Run the ones the drift policy lets you run and leave the others pending;
      a script you hold back never blocks the rest.
   5. Config: if it is still stale and the drift policy allows it, `chezmoi init`, with the
      backup already taken. Confirm no machine-only key was lost.
   6. Publish if the user agreed: `chezmoi git -- push`.
7. **Verify.** Run `${CLAUDE_SKILL_DIR}/facts.sh` again. Anything left over gets one more pass.
   After three passes, stop and report what remains and why.
8. **Report.** Your final message is the report below and nothing else: it starts with the
   status line, with no sentence before it and no narration of what you ran.

## Report

Start with the status line from a fresh run of the facts (or `✅ in sync`). Then:

```
🛟 <backup directory>          (only when a backup was taken)

| | Item | Action | Why |
|---|---|---|---|
| ⬆️ | `~/.notes` | captured into `dot_notes` | hand edit, source unchanged |
| 🧩 | `~/.gitconfig` | `editor` set in the local config | machine-specific value on a templated line |
| 🔀 | `~/.profile` | merged both sides | different hunks |
| ⬇️ | `settings.toml` | applied | source ahead |
| ♻️ | `~/.cache.json` | restored from source | tool churn |
| 🔇 | `~/.config/gh/hosts.yml` | added to `.chezmoiignore` | rewritten by `gh` on every login |
| ▶️ | `install-tools.sh` | ran | idempotent, user-space |
| ⚙️ | `chezmoi.toml` | regenerated | only additions |
| 📝 | source | committed `<hash>` | |
| 🚀 | source | pushed | you agreed |
| ❓ | `~/.zshrc` | `<the option you chose>` | you decided |
| ⏭️ | `<item>` | left as is | `<why>` |
```

One row per item, `Why` at most 8 words, items in the order you handled them. Rows only for
what happened. If anything remains unclean, list it as ⏭️ rows and say nothing more.
