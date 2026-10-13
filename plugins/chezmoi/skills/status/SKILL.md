---
name: status
description: Use when the user asks about the state of their chezmoi dotfiles, here or on a remote host (`--ssh <host>`) - "chezmoi status", "are my dotfiles in sync", "what's drifted", "do I have anything to push", "anything to pull", "did my dotfiles change", checking before a machine handover or reinstall, or diagnosing why an expected dotfile change has not appeared. When invoking this through the Skill tool, reproduce the returned report verbatim in your reply and add nothing to it.
argument-hint: "[--ssh <host or ssh command>]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/report.sh:*)
context: fork
model: haiku
background: false
---

# chezmoi status

Render the chezmoi status report for the user. Arguments, if any: $ARGUMENTS

Your final message is pasted verbatim to the user and is their whole view. It starts with the status line. It contains only what the layout below allows: no preface, no verdict, no summary, no mention of the facts, tools, method or these instructions, no corrections, no caveats, no sentences except the ones the layout names. Run no commands.

If the facts are the single line `✅ in sync` (possibly ending with ` · 🌐 <host>`), reply with exactly that line.

## Facts

!`${CLAUDE_SKILL_DIR}/report.sh $ARGUMENTS`

Lines marked `(copy verbatim)` are copied character for character. Everything else is raw material you interpret. Replace the home directory with `~`.

## Layout, in this order

1. The STATUS_LINE value, alone on the first line, exactly as given: nothing added before or after, no backticks, no bold.
2. One entry per `### FILE` block, then the `### CONFIG` block, in the order given. Entries are separated by one blank line.
3. One entry per `### SCRIPT` block.
4. The LEGEND value, when the facts have one, copied exactly as one single line: do not rewrite, split, shorten or add to it.
5. `## 🗂️ Repo`, then `## ⚠️ Watch`, then `## ➡️ Next`, each only if it has content. A heading is followed directly by its content, with no blank line.
6. The OFFER value, alone on the very last line, after one blank line, copied exactly. Every reply whose facts contain an OFFER value ends with it, whatever else the reply contains.

### Errors

When the facts contain `### ERRORS`, the status line is followed by `## ❗ Errors` and, directly under it, one bullet per error line with its text in backticks.

### File and config entries

Heading line: `<emoji> **`<path>`**`, then the `file_delta` number in backticks, then its mark outside the backticks. Example: `📄 **`~/.bashrc`** `−1` ⚠️`. If `file_delta` is `none`, the heading has only the emoji and path.

The table starts on the very next line: never put a blank line between the heading line and its table. Separator rows are written exactly `|---|---|` (no spaces).

Exact shape of an entry (the heading and the table are adjacent lines, and every Item, even `line 2`, is in backticks):

```
📄 **`~/.bashrc`** `−1` ⚠️
| Δ | Item | Disk | Apply |
|---|---|---|---|
| ➖ ⚠️ | `alias gs` | present | ∅ |
```

A file whose facts say `contents: hidden` has only a heading line: its emoji, the path, and the `file_delta` value if there is one (a mark only, no number). It has no table.

Columns: plain and encrypted files use `Δ | Item | Disk | Apply`. Templates and the config use `Δ | Item | Disk | Template | Apply | Re-init`; when the facts contain no `reinit_diff` and no `### CONFIG` block, templates drop the Re-init column (`Δ | Item | Disk | Template | Apply`) and no 🔁 appears.

One row per changed item, ordered by line number. The Item is always in backticks: for an `alias name=value` line it is `alias name`; for a `key = value` or `key=value` line it is the key (`mode`); only when neither applies, `line N`. A `[section]` header line never gets a row of its own: it belongs to the entries under it, and when the header itself is one of the changed lines the item is `[section] key` (`[alias] co`). Do not prefix the section for a key that already exists in its section.

How to read the diffs: in `apply_diff`, `-` lines are on disk now and `+` lines are what apply writes. A `-` line and a `+` line at the same position are one replaced item. A lone `-` line is removed by apply. A lone `+` line is added by apply. In `reinit_diff`, `-` is the apply result today and `+` is the apply result after re-init. Merge rows that describe the same line.

Cells:
- Δ: the symbol, a space, then the mark if any (`➖ ⚠️`, `✏️ 💥`). Symbols: ✏️ replaced by apply · ➖ removed by apply · ➕ added by apply · 🔁 changes only on re-init (it appears in `reinit_diff` and not in `apply_diff`). When the file mark is ⚠️, add ⚠️ on every row whose Disk value differs from Apply; when it is 💥, add 💥 instead. 🔁 rows never get a mark.
- Disk: the value on disk. For `key = value` and `key=value` lines show the value only (for `alias zz=clear`, `clear`), keeping its quotes, in backticks. For other lines show the whole line in backticks. A lone removed item, whether a whole line or an entry in a section, is the plain word present (no backticks), never its content. An added item is ∅ (no backticks).
- Apply: the value apply writes, formatted like Disk. A removed item is ∅.
- Template (templates only): the template expression behind the value, from `template_source`, in backticks, with `|` written as `\|`, only when the item's template line contains `{{ }}`; otherwise leave the cell empty.
- Re-init: the value after re-init, only when it differs from Apply; otherwise leave the cell empty.
- Config entries: take rows from `changes`: symbol and Item as given, Item in backticks, Disk = the old value (`(absent)` becomes ∅), Template empty, Apply — (no backticks), Re-init = the new value.

A `### CONFIG` block that says `could not compare` is rendered as the heading `⚙️ **`config`**` followed directly by the line `❗ could not compare` and the first message line in backticks.

### Script entries

Three lines, no blank line between them, one blank line between entries:

```
▶️ **`<script name>`**
<tag, copied verbatim>
Will <verb> <what it does, at most 10 words, no trailing period>
```

Quote any literal text the script prints in backticks, and write the home directory as `~`: `Will print `installing tools``.

### Repo

A table with an empty header (`| | | |`): row `📝 | uncommitted | <files in backticks joined by " · ">`, row `⬆️ | unpushed | <hash in backticks> <subject>` entries joined by ` · `, row `⬇️ | unpulled (last fetch) | …` the same way, and `🔗 | upstream | none` when there is none. If the facts report uncommitted files also touched by an unpulled commit, end the unpulled cell with ` — touches `<file>`, also edited locally`. Omit rows with no data.

### Watch

`## ⚠️ Watch` is followed directly by bullets, no blank line. Only these bullets, each as a sentence of the given form ending with a period, in this order, omitting those that do not apply:
- for each 💥 file: `<path>` 💥: `re-add` loses `<its source-side line>`; `apply` loses `<its disk-side line>`.
- for each template with a ⚠️ mark: `<path>` is a template: `re-add` is ignored, edit `<source file>`.
- for each uncommitted file also touched by an unpulled commit: `<target path of that file>`: commit your edit before pulling `<hash>`.
- for each source-ahead template that re-init also changes: `<base name of the file, e.g. settings.toml>`: apply it before regenerating the config, or `<its 🔁 keys>` flip too (each key in its own backticks, joined with the word and).

### Next

When the facts contain a REMOTE value (a flag such as `--ssh home`), the Next items are for that machine: write every `chezmoi:<skill>` as `chezmoi:<skill> <flag>` and every `chezmoi <command>` in items 2, 4 and 6 as `czr <flag> chezmoi <command>`; item 1's phrases and the OFFER stay exactly as given.

`## ➡️ Next` is followed directly by a numbered list. Only these items, in this order, omitting those that do not apply, with no trailing period on any item:
1. The text `Decide the hand edits: ` followed, for each ⚠️ and 💥 file in the order of the entries, by `<path>` and in parentheses one of these three phrases written exactly: `chezmoi re-add` keeps it · edit `<source file>` · merge into `<source file>`. Use the first for plain and hidden files, the second for templates, the third for 💥 files. Separate the files with commas; the whole is a single item and the parentheses contain only the phrase.
2. `chezmoi git -- add <uncommitted files>`, then commit.
3. `chezmoi:update` — rebase onto `<hash of the last unpulled commit>`.
4. One item per source-ahead template that re-init also changes: `chezmoi apply <path>`.
5. `chezmoi:setup` — regenerate the config.
6. `chezmoi apply` — runs <both scripts | the script | N scripts>.
7. `chezmoi:push`, only when there are unpushed commits or uncommitted files; never when the facts show neither
