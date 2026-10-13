# chezmoi drift policy

Used by `chezmoi:resolve`, on top of `conflict-policy.md` (which still decides every source-repo
conflict). This file decides what to do with each unclean **aspect** of the status, and which
ones are worth a question.

Three versions of every deployed file matter. **B** is what chezmoi last wrote (the base,
recovered by `base.sh`), **D** is the file on disk, **S** is what the source renders to now.
`facts.sh` gives each file a class from these.

## Per-aspect decisions

| Class | Situation | Do | Ask? |
|---|---|---|---|
| `source-only` | D = B, S ≠ B | `chezmoi apply <path>` | no |
| `disk-only` | D ≠ B, S = B, plain file | capture: `chezmoi re-add <path>` | no |
| `disk-only` | D ≠ B, S = B, templated line | port the edit to the right layer (below) | no |
| `disk-only` | D ≠ B, S = B, tool churn (see signals) | restore from source with `chezmoi apply <path>`; add to `.chezmoiignore` when it recurs | no |
| `both-changed` | merge preview is clean | write the merged text into the source file, then apply | no |
| `both-changed` | both made the same change | nothing to merge; apply | no |
| `both-changed` | same line, two different values | a decision: usually it wants to be a template | yes |
| `deleted-on-disk` | file removed on disk, source unchanged | intent is unknown | yes |
| missing, never applied | no state, not on disk | `chezmoi apply <path>` | no |
| `rendering-changes-on-re-init-only` | only the config would change it | nothing until the config is handled | no |
| script pending | idempotent, user-space (prints, symlinks, caches, generated files) | let `apply` run it | no |
| script pending | `sudo`, package installs, network fetches, `rm -rf`, anything outside `$HOME`, or any `run_once_` change | read the full body, then ask | yes |
| config stale | only additions, or values the template computes | back the config up, `chezmoi init` | no |
| config stale | regenerating would drop a key that only exists on this machine | a decision | yes |
| source uncommitted | | commit, in the repo's style | no |
| source behind | | `fetch`, then `rebase`, conflicts per `conflict-policy.md` | per that policy |
| source ahead | | publish | once, see below |

Anything in the "put it to the user" table of `conflict-policy.md` (credentials, delete versus
modify, restructuring, a changed `run_once_` script) is a question here too.

## Which layer an edit belongs to

A hand edit to a rendered file is an edit to the wrong layer when a template or a data key
produces that line. Find the layer from `template_source` and `chezmoi data`:

1. **Literal template line** (no `{{ }}`): edit that line in the template.
2. **Line driven by a `[data]` key** (`{{ .editor }}`) and the new value is specific to this
   machine: set the key in the local config (`chezmoi data --format json` shows the config
   file path). The template and the other machines stay untouched. Prefer this over a
   hostname branch.
3. **Line driven by a key but the value is a deliberate change for all machines**: edit the
   template or the shared data file; this restructures the source, so it is a question.
4. **Plain file**: `re-add`.

After any port, `chezmoi cat <path>` must equal the file on disk. If it does not, the port is
wrong; fix it before moving on.

## Signals for intent

Use them to decide, and say which one decided in the report.

- **Tool churn**: timestamps, reordered keys, rotated tokens or caches, a file a tool rewrites
  on its own (`gh` rewriting `hosts.yml`). Restore from source; do not capture it.
- **Deliberate edit**: a coherent change a person would type: an alias, a setting, a path.
  Capture it.
- **Machine-specific value**: a hostname, a username path, an email, a mount point, a port.
  Keep it local (data key), not shared.
- **Source history** (`recent source history` in the facts): a recent commit on the same lines
  from another machine means the source side is deliberate too.
- **Modification times** only break ties. They never decide alone.

## Publishing

Everything above is local and reversible. Pushing is not: it changes what every other machine
receives on its next update. So resolve never pushes silently. Ask one yes/no in the same
`AskUserQuestion` call as the other decisions (or right after, when that call is full), unless
the user passed `push`. A declined push still ends with the source committed locally.

## Safety net

Run `backup.sh` before the first overwrite, naming every deployed file `apply` will touch and
the config file if it will be regenerated. Report the directory it prints. Source changes are
already recoverable through git.

## Never

- `chezmoi apply --force` as a blanket way to make drift vanish, or on a path you have not decided about.
  Once you have decided what happens to a file and `backup.sh` has copied it, `chezmoi apply --force <path>`
  for exactly that path is the right way to overwrite it. Without it chezmoi prompts on any file that changed
  since it last wrote it, and in a non-interactive session there is no terminal to answer.
- `chezmoi merge`, `vimdiff` or any interactive tool.
- Capturing a file whose contents are hidden (private or encrypted) without telling the user
  what category it is; the diff is not visible, so say that the decision rests on the file's
  class and not on its content.
- Running a script you have not read.
- Resolving a hunk you have not read.
- Leaving conflict markers anywhere. `chezmoi git -- grep -n '^<<<<<<<'` before every commit.
