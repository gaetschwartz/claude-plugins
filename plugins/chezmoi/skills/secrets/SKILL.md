---
name: secrets
description: Use when a dotfile contains or needs a token, password, API key or private key, when `chezmoi add` warns it found a secret, or when deciding how chezmoi should store a credential.
---

# Secrets

The source repo is pushed and shared by every machine. A secret in a plain source file
is published.

## Follow the repo's existing convention

Check: `.chezmoiignore`, any `encrypted_*` sources, and
`rg -l 'bitwarden|onepassword|keyring|pass |rbw' "$(chezmoi source-path)"`.
If the repo has no convention yet, choosing one is a design decision: ask the user.

| Approach | How | Trade-off |
|---|---|---|
| Local only | put the secret in its own file, list that file in `.chezmoiignore`, and have the managed file `source`/include it | not synced; set up by hand per machine |
| Password manager | a template reads it: `bitwarden`, `bitwardenFields`, `onepasswordRead`, `rbw`, `pass`, `keyring` | manager must be unlocked at every apply |
| Encryption | `chezmoi add --encrypt` (age or gpg set in config) | key needed on every machine |

## Rules

- Never write a secret into a source file, `.chezmoidata`, or a commit message.
- If one already reached the source: remove it, and tell the user it is in git history
  and should be rotated. Rewriting history is their call.
- If `chezmoi apply` or `diff` waits for a master password, stop and ask the user to
  unlock. Use `--skip-secrets` for read-only checks; it skips those templates.
