---
name: setup
description: Use when setting up chezmoi dotfiles on a new machine, when chezmoi warns "config file template has changed", when editing .chezmoi.toml.tmpl, or when adding a per-machine setting that should be asked once.
---

# Setup and config

## New machine

`chezmoi init <repo>`, then `chezmoi diff`, then `chezmoi apply`: don't skip the preview
that `init --apply` skips. Read every `run_` script that `chezmoi status` shows as `R`
before applying; they execute.

## Config comes from a template

`.chezmoi.toml.tmpl` at the source root renders into `~/.config/chezmoi/chezmoi.toml`.
Edit the template, never the generated file: the next `init` overwrites it.

After changing the template, chezmoi warns "config file template has changed, run chezmoi
init". Regenerating rewrites the whole file, so preview it first.

`chezmoi init --dry-run --verbose --no-tty </dev/null` prints the diff of the config it would
write and changes nothing. Run it only where the source is already a git repository: with
none, even `--dry-run` creates one. An unanswerable prompt fails it with the question; the
`--no-tty` flag makes a prompt read stdin, so close stdin or it hangs.

A `-` line with no `+` counterpart is a key set only on this machine, and regenerating drops
it. Templates that read it then fall back to their default or fail, and a guarded
`hasKey` fallback changes the rendered file without any error. `chezmoi:status` shows both the
diff and the effect on the managed files. Move such keys into the template before
regenerating.

`--init` on `status`, `diff` and `apply` does not preview this: it keeps the old data keys
visible to templates, so it understates what a real `init` leaves behind. To see the real
effect, render the template with `chezmoi execute-template --init` into a file and point
`--config` at it.

Then regenerate with `chezmoi init`, or `chezmoi apply --init` to do both.

## Prompts

`promptStringOnce . "email" "Email"` asks once and reuses the stored value on every later
`init`. With no terminal, prompts fail with EOF. Answer them with flags keyed by the
**prompt text**, not the data key:

    chezmoi init --promptString Email=me@example.com --promptBool "Work machine=true"

`--promptDefaults` takes each prompt's default. Never invent an answer the user has to
give (email, work vs personal): ask.
