---
name: edit
description: Use when adding a file to chezmoi, changing a chezmoi-managed dotfile, making chezmoi stop managing a file, or when a chezmoi command fails with "could not open a new TTY".
---

# Changing managed files

Every managed file exists twice: the **source** (`chezmoi source-path <path>`) and the
deployed file in `$HOME`. Each command moves data one way:

| Command | Direction | Use for |
|---|---|---|
| `chezmoi add <path>` | home → source | start managing a file |
| `chezmoi re-add <path>` | home → source | capture an edit made to the deployed file |
| edit the source file, then `chezmoi apply <path>` | source → home | change a managed file |
| `chezmoi forget <path>` | removes the source only | stop managing, keep the file |
| `chezmoi destroy <path>` | removes source **and** the home file | delete everywhere |

Edit source files directly with your normal tools. Don't use `chezmoi edit`; it opens an
interactive editor.

## Check the source type before add or re-add

`chezmoi source-path <path>`:
- ends in `.tmpl`: `re-add` silently skips it, and `add` asks to remove the template
  attribute. With `--force` it replaces the template with the rendered file. Port the
  change into the template by hand instead (`chezmoi:templates`).
- otherwise, including `encrypted_` sources, `re-add` is safe; it re-encrypts on its own.

## Adding new files

- `private_`/`executable_` come from permissions on disk, per entry: `chmod` first.
- `add` only *warns* when it detects a token and adds the file in plain text anyway. Use
  `chezmoi add --secrets=error`; if it trips, follow `chezmoi:secrets`.
- Adding a directory adds everything under it.
- Symlinks are added as symlinks; `--follow` adds the file they point to.

## "could not open a new TTY"

chezmoi asked a yes/no question and there is no terminal. The line before the error is the
question, and it is a safety check ("would remove template attribute", "has changed since
chezmoi last wrote it"). Resolve what it is warning about. Never add `--force` to make it
go away: that answers yes to overwriting.

## After

`chezmoi diff <path>` should be empty. The change is only local until pushed
(`chezmoi:push`).
