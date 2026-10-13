# Working on a remote host

Used when the skill's arguments contain `--ssh <ssh>`. `<ssh>` is either a host name (`home`, run
as `ssh home`) or a whole command (`ssh -p 2222 me@box`). Without `--ssh` nothing here applies.

The target is the remote machine's chezmoi: its home, its source repository, its config. This
machine is only the one you type on.

## What runs where

| Need | Command |
|---|---|
| facts, diffs, base text, backups | `${CLAUDE_SKILL_DIR}/<script>.sh --ssh <ssh> ...`. The script ships itself to the remote and runs there. |
| any chezmoi command | `czr --ssh <ssh> chezmoi <args>`, in place of `chezmoi <args>`. Chain the git commands the same way: `czr --ssh <ssh> chezmoi git -- log --oneline`. |
| read a file | `czr --ssh <ssh> get <path>` |
| write a file | `czr --ssh <ssh> put <path>`, content on stdin (a heredoc) |

`~/` in any argument means the remote home. A path under this machine's home is read as the same
path under the remote one.

`Read`, `Edit` and `Write` only touch this machine. To change a source file on the remote, `get`
it, work out the new text, `put` the whole file back, then check with `czr ... chezmoi git -- diff`.

## Limits

- `czr` runs chezmoi and nothing else. It reads and writes only the chezmoi source and config
  directories, plus reads of managed targets. Never `.git`, and never `private_` or `encrypted_`
  entries. If a task needs more than that, tell the user instead of looking for another route.
- Never run a bare `chezmoi ...` or a raw `ssh` for the remote: the first acts on this machine,
  the second is not allowed.
- Connections do not prompt (`BatchMode`) and time out after 10 s. When the host is unreachable
  the facts say `cannot reach <host>: <reason>`; report that line and stop. Do not retry with other
  options.
- The remote needs bash, chezmoi, git, jq, tar and python3 3.11 or newer.

## In reports

Keep the `🌐 <host>` label that the status line carries, and name the host in anything you say
about paths: `~/.zshrc` on `home` is not `~/.zshrc` here. Backups taken with `backup.sh` live on
the remote; say so.
