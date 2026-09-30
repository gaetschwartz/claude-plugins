---
name: diff
description: Use when the user wants to see how a chezmoi-managed file differs from its source - "check the diff of <file>", "what changed in <dotfile>", "what would chezmoi apply change", "why is <dotfile> different from source", or after a drift notice flags a file. When invoking this through the Skill tool, reproduce the returned diff blocks verbatim in your reply and add nothing to them.
argument-hint: "[path]"
allowed-tools: Bash(chezmoi:*), Bash(${CLAUDE_SKILL_DIR}/diff.sh:*)
context: fork
model: sonnet
background: false
---

# chezmoi diff

Show how chezmoi-managed files differ from their source. Path, if any: $ARGUMENTS

Drifted entries and their diffs, captured when this skill loaded. Each block starts with a
`==` marker line for you — the user never sees it. No path means every drifted entry.

!`${CLAUDE_SKILL_DIR}/diff.sh $ARGUMENTS`

## Reading a block

The marker gives the drift direction and the diff was already taken in that direction:

| Marker | Meaning | `-` lines | `+` lines |
|---|---|---|---|
| `dest-ahead` | deployed file edited since chezmoi wrote it | source | live |
| `source-ahead` | source moved, apply pending | live | source |
| `script` | script would run on apply | — | — |

`(no difference)` means nothing drifted — say exactly that and stop.

## Rendering

Your final message is the user's whole view. Per entry, in this order:

1. The path in bold, `~`-shortened.
2. A two-line legend in a ```` ```diff ```` fence, `-` line first, colour-matching the diff. Name the
   side the way the user thinks of it, never the direction or flag: `- source   <source path>`
   / `+ live     <~ path>` for `dest-ahead`, `- live ...` / `+ source ...` for `source-ahead`.
   Show the source path relative to the chezmoi source dir.
3. The change in a second ```` ```diff ```` fence, one row per line: `<mark> <lineno> │ <text>`,
   where `<mark>` is `-`, `+`, or two spaces for context. Drop `diff --git`, `---`, `+++` and
   `@@` lines entirely. Number each line from the `@@` headers — removed lines by their own
   side, context by the live side. Keep 2 context lines around a change and put `⋮` between
   distant hunks. If there are more than 5 hunks or you are unsure of the arithmetic, omit the
   numbers rather than risk wrong ones.
4. At most one footer line, only when it names an action:
   - `dest-ahead`, `.tmpl` source → `→ edit <source path>`
   - `dest-ahead`, plain file → `→ chezmoi re-add <~ path>`
   - `source-ahead` → `→ chezmoi apply <~ path>`

Fidelity beats polish: keep every changed line, preserve indentation exactly, never merge,
reword or drop a line. If the block says `(truncated: N more lines)`, say so and give
`! chezmoi diff <path>` (append `--reverse` for `dest-ahead`) so the user can see the rest. A
`script` entry, or a mode-only, new or deleted entry, gets one line saying so instead of a diff.
For `dest-ahead` entries, first check `status='..'`: `MM` means applying would also overwrite the
edit, so end with the `re-add` or edit line and never suggest `apply`.

No other prose. Never run a mutating command.

For the overall picture — sync position, unpushed work, pending scripts — point at
`chezmoi:status`.
