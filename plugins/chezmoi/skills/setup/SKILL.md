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
init". Regenerate with `chezmoi init`, or `chezmoi apply --init` to do both.

## Prompts

`promptStringOnce . "email" "Email"` asks once and reuses the stored value on every later
`init`. With no terminal, prompts fail with EOF. Answer them with flags keyed by the
**prompt text**, not the data key:

    chezmoi init --promptString Email=me@example.com --promptBool "Work machine=true"

`--promptDefaults` takes each prompt's default. Never invent an answer the user has to
give (email, work vs personal): ask.
