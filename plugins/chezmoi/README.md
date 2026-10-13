# chezmoi

chezmoi dotfile-manager expertise for Claude Code, plus a drift advisor hook.

## Skills

| Skill | Use it for |
|---|---|
| `chezmoi:update` | Pulling changes into a machine. Drives the fetch → inspect → integrate → apply sequence explicitly instead of the opaque `chezmoi update`, settling destination drift *before* apply so local edits aren't silently overwritten. |
| `chezmoi:push` | Publishing changes. Captures uncaptured destination edits first, commits, pushes, and on rejection integrates rather than forcing — then reports what was pushed, what came back, and what changed locally. |
| `chezmoi:status` | Answering "is anything drifted / do I need to push or pull". Loads the live picture up front so the answer needs no tool round-trips. A script gathers the facts and a Haiku agent lays them out as a fixed report: one table per drifted file (disk, template, what `apply` writes, and what a config re-init would change), pending scripts, repo position and the next steps, or a single `✅ in sync`. |
| `chezmoi:diff` | Showing how a drifted file differs from its source, as a readable line-numbered diff drawn in the direction that actually drifted. |
| `chezmoi:resolve` | Bringing a machine back to a clean status in one pass. Reads the same facts as `chezmoi:status` plus the last-written base of each file, then captures hand edits, merges disjoint changes, applies source updates, commits, rebases and runs safe scripts itself. It asks only when it is genuinely unclear which side to adopt (conflicting values, a file deleted on disk, a script with `sudo` or installs, a config regeneration that would drop machine-only keys) and before publishing. Backs up every file it will overwrite first. |
| `chezmoi:hook` | Turning the drift hook off or on, and reading its state. |
| `chezmoi:edit` | Adding, changing and removing managed files: which way each command moves data, and the template/encrypted exceptions to `re-add`. |
| `chezmoi:templates` | Making a file vary per machine, and testing a render before applying. |
| `chezmoi:secrets` | Keeping credentials out of the shared source repo. |
| `chezmoi:setup` | New-machine init, the config template, and answering prompts without a terminal. |

### Embedded shell execution

The status, diff, update, push and resolve skills use skill-injected commands — `` !`…` `` in the body, pre-approved with
`allowed-tools` — so live state is already in context when the model starts reasoning,
instead of costing several tool calls. The `status` facts take about 0.4 s when everything is in sync, and up to about 1.5 s on a drifted tree with a stale config.

A reference skill is pending a rewrite; see the note at the end of this file.

If you edit these: a non-zero exit from an injected command **aborts the whole skill
invocation**, and injected commands never prompt for permission, so an `ask` or `deny` rule
aborts too. Guard every command so it exits 0 and prints something explicit when there is
nothing to report.

### Remote hosts

`status`, `diff`, `resolve`, `update`, `push`, `setup` and `edit` take `--ssh <ssh>`: a host name
(`--ssh home`, run as `ssh home`) or a whole command (`--ssh "ssh -p 2222 me@box"`). The target is
the remote machine's chezmoi: its home, source repository and config.

- The fact scripts ship themselves as a tar over the same connection, run there, and print their
  facts here; the status line carries `🌐 <host>` and the offered next steps carry the flag.
- Agent-driven steps go through `bin/czr`, which runs chezmoi and nothing else, and reads or
  writes only the remote's chezmoi source and config directories (never `.git`, never
  `private_` or `encrypted_` entries). So `Bash(czr:*)` is all the skills need pre-approved, not `Bash(ssh:*)`.
- Connections never prompt (`BatchMode`) and time out after 10 s. The remote needs bash, chezmoi,
  git, jq, tar and python3 3.11+. The drift hook and the knowledge-only skills stay local.

### Conflict handling

`chezmoi:update` and `chezmoi:push` share one policy, in
`skills/chezmoi/references/conflict-policy.md`. Its premise is that **a chezmoi source repo
is shared across machines**, so taking one side of a conflict rewrites what every other
machine receives.

The agent resolves conflicts itself and escalates only genuine design decisions — a value
that differs deliberately on two machines, a delete-vs-modify, a credential divergence, a
changed `run_once_` script, or a fix that would restructure the source. When it does
escalate it must present named options with their consequences and a recommendation. It is
never allowed to hand a conflict back with "please resolve this", and never leaves conflict
markers behind.

## The drift hook (on by default)

chezmoi keeps two states — the source (`~/.local/share/chezmoi`) and the destination
(`$HOME`) — and changing either leaves the other stale.

The hook runs **`chezmoi status`** before and after `Bash`, `Edit`, `Write` and
`NotebookEdit`, and reports only what changed in between.
Asking chezmoi itself, rather than inspecting the tool's arguments, is what makes it
reliable: a dotfile can be changed by a shell redirect, `sed -i`, `tee`, a Python
script, or an installer, and none of those name a file path the hook could inspect.
`chezmoi status` sees all of them equally. About 170 ms per tool call in total.

Drift that already existed stays quiet. When two sessions run tools at the same time, a
change goes to the session whose Edit/Write named the file, else to whichever session had
a tool running at the file's modification time, else to the one whose command mentions
the path. Anything short of an exact Edit/Write match ends with "if you didn't change
these files, ignore this message".

What it adds beyond "run chezmoi status":

| Situation | Advice given |
|---|---|
| Destination file edited | `chezmoi re-add <path>` to persist, or leave it to be overwritten |
| …source is a **template** | Warns `re-add` silently skips templates; says to port the edit into the template, then `apply` |
| `git.autoCommit`/`autoPush` on | Warns the suggested command will also commit and push |
| Source ahead of destination | Names what `apply` would create, delete or overwrite |
| Script pending | Flags that `apply` would run it |

It never runs a mutating chezmoi command, caps output at 8 entries, and exits quietly
if `chezmoi` or `python3` is missing or chezmoi is unconfigured.

Disable it with the `chezmoi:hook` skill, or `CHEZMOI_DRIFT_HOOK=0` in the environment.

## Install

```
/plugin marketplace add gaetschwartz/claude-plugins
/plugin install chezmoi@gaetans-claude-plugins
```
