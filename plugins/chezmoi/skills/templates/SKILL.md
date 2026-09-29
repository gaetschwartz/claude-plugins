---
name: templates
description: Use when a chezmoi-managed file must differ between machines (OS, hostname, work vs personal), when editing a .tmpl source, or when a chezmoi template renders wrongly or fails.
---

# Templates

A source ending in `.tmpl` is rendered with Go `text/template` + sprig on every apply.

## Template only what varies

To have a whole file only on some machines, list it in `.chezmoiignore` (itself a
template, paths relative to `$HOME`) rather than wrapping the file in `{{ if }}`.

## Making a file a template

- New file: `chezmoi add --template <path>`, then replace the varying parts.
- Already managed: `chezmoi chattr +template <path>`.

## Data

- `chezmoi data` lists everything available: `.chezmoi.os`, `.chezmoi.hostname`,
  `.chezmoi.arch`, `.chezmoi.username`, plus user data.
- Per-machine values: `[data]` in the config, generated from `.chezmoi.toml.tmpl`
  (`chezmoi:setup`). Shared values: `.chezmoidata.{toml,yaml,json}` (never templated).
- Shared snippets: `.chezmoitemplates/<name>`, used as `{{ template "<name>" . }}`.

## Test without applying

- `chezmoi execute-template < <source>.tmpl` renders a file.
- `chezmoi cat <path>` shows what apply would write.
- `chezmoi diff <path>` shows what apply would change in the current file.

## Footguns

- A template that renders empty **deletes the target** on apply. Prefix the source with
  `empty_` if the file must exist anyway.
- A missing key is an error, and `| default` does not rescue it. Guard with
  `{{ if hasKey . "work" }}`.
- `re-add` skips templates. An edit made to the deployed file has to be ported into the
  template by hand; `chezmoi diff --reverse <path>` shows the edit as `+` lines.
