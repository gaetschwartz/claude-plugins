---
name: status
description: Use when the user asks about the state of their chezmoi dotfiles - "chezmoi status", "are my dotfiles in sync", "what's drifted", "do I have anything to push", "anything to pull", "did my dotfiles change", checking before a machine handover or reinstall, or diagnosing why an expected dotfile change has not appeared.
allowed-tools: Bash(chezmoi:*), Bash(${CLAUDE_SKILL_DIR}/state.sh:*)
context: fork
model: sonnet
background: false
---

# chezmoi status

Report the state of the user's chezmoi dotfiles. Focus, if any: $ARGUMENTS

The state below was captured when this skill loaded. Only the parts with something to report
are printed: a missing section means that part is fine, and a single `(in sync …)` line means
everything is. **Read it before running anything** — in most cases it already answers the
question and no further commands are needed.

!`${CLAUDE_SKILL_DIR}/state.sh`

## Reading the sections

| Section | What it tells you |
|---|---|
| `destination drift` | Deployed files that disagree with the target state. Each line ends with the source kind (`plain`, `template`, `encrypted`) and its path, which decides how to capture an edit. |
| `chezmoi status errors` | chezmoi could not evaluate something, so the drift list may be incomplete. Quote the error. |
| `encryption` | An encrypted entry drifted. `re-add` re-encrypts it on its own. |
| `source repo: uncommitted changes` | Local edits in the source tree not yet committed. |
| `sync position` | Commits not pushed or not pulled, or no upstream. Counts come from the last fetch. |
| `capturing commits` | `autoCommit`/`autoPush` is on: `add` and `re-add` also commit and push. Say so when you advise capturing. |
| `config: init would write` | The diff of the config file `chezmoi init` would write, current file against regenerated. `-` lines exist today, `+` lines would exist after. A `-` line with no `+` counterpart is a key set on this machine that the template does not produce. |
| `config: effect of regenerating on managed entries` | What regenerating the config would do to the tree: each entry's status now and after, and for each changed file the rendering under the current config (`-`) against a regenerated one (`+`). |
| `config: cannot preview` | chezmoi's own message for why the config could not be compared (an unanswerable prompt, or a chezmoi panic). Report it as the reason, not as "no drift". |

`destination drift` columns: the first is "the destination changed since chezmoi last wrote
it", the second is "what apply would do". `MM` means an edit exists locally **and** apply would
overwrite it. ` M` means the source moved ahead and applying is safe.

## Answering well

Lead with the verdict — in sync, or the specific thing that is not — then the detail. Do not
re-run the commands above to restate what is already here.

For each drifted entry, the advice depends on its source kind: `plain` and `encrypted` take
`chezmoi re-add`; a `template` source silently ignores `re-add` and must be edited in the
template.

Report `R` entries explicitly — a pending script runs code on the next apply.

### Config drift

Treat it as part of the picture, not a footnote. Say which managed files would render
differently if the config were regenerated and what the difference is — in particular a value
that silently falls back to a default, or a key that stops being set. The numbers in these
sections are computed from a rendered config, so state them as fact.

Do not tell the user to run `chezmoi init` or `chezmoi apply --init`: regenerating overwrites
the generated config and drops keys set only on this machine. Describe the impact, then ask
whether they want the main agent to resolve it with the `chezmoi:setup` skill.

Do not use the `--init` flag of `status`, `diff` or `apply` to estimate the impact. It keeps the
old data keys visible to templates and understates what a real `init` leaves behind.

### Never mutate

Never run a mutating command (`apply`, `re-add`, `add`, `init`, `destroy`, any `git` write).
Name the command that would fix each issue; the caller decides whether to run it.

## Going further

Where relevant, point the caller at `chezmoi:update` (pull and integrate remote changes),
`chezmoi:push` (capture, commit and publish local changes), `chezmoi:diff` (the content of a
specific drift) or `chezmoi:setup` (the generated config).
