# Presentation conventions

Claude Code cannot show a command's output in the chat by itself: what the user reads is the text you write. So the
CLI prints the final markdown, computed from real results, and you paste it.

## The verbatim-paste rule

- Run the render command, then copy its output into your message VERBATIM: unchanged, character for character, no
  paraphrase, no reordering, no added or removed rows, no extra prose inside the block. Only the surrounding text the
  skill prescribes (a question, the `Cause` lines of `explain`) goes outside it, separated by a blank line.
- Never write a script, and never do arithmetic by hand, to compute spacing, rows or verdicts. Never type a verdict, a
  `wrapped` tag or a count yourself: they come from the CLI, which computes them from the engine's own verdicts.
- The output is unfenced markdown. Do not wrap it in a code fence, a quote or a table, and do not indent it. (The
  examples below are indented four spaces only so this file can show them.)
- A wrong row means a wrong input or a wrong rule: fix the input (the examples, the rule) and run the command again.
  Do not edit the output.

## Rule card: `guardrails rule test`

    guardrails rule test --json @rule.json --examples @examples.json --intent '<text>' --id-name <id> --scope <s>
    guardrails rule test --id <id> --examples @examples.json

Inputs: the rule (`--json`, or an installed one with `--id`; `--scope` and `--id-name` only label a draft), the commands
(positional, source `inferred`; `--source` changes that default), and `--examples @file` or `--examples -` (stdin), a
JSON list of objects:

    [{"cmd": "sudo pkill -f vite", "source": "inferred", "expect": "match"}]

- `source`: `yours` (the user's own example or description), `inferred` (you elaborated it and it was unambiguous),
  `you chose` (decided in an edge-case question). Default `inferred`.
- `expect`: `match` (the user wants it caught) or `pass` (the user wants it to go through). Optional; without it the
  row is never a mismatch. Set it from what the user said, not from what the matcher did.
- `cmd`: the exact command text; a newline in it is fine.

Output:

    ### no-pkill · deny · retry same-command · global

    **Intent** stop killing processes by name, suggest kill by PID
    **Match** program = `pkill`, `killall`
    **Message** Killing by name can hit the wrong process. Find the PID with pgrep -fl, then kill it by PID.

    **Block**
    - ✗ `pkill node                              ` yours
    - ✗ `sudo pkill -f vite                      ` inferred · wrapped
    - ✗ `bash -c 'killall Safari'                ` inferred · wrapped
    - ✗ `killall Safari                          ` inferred
    - ✗ `pkill -0 node                           ` you chose
    - ✗ `` echo `pkill x`                           `` inferred · wrapped

    **Allow**
    - ✓ `kill 4242                               ` yours
    - ✓ `pgrep -fl node                          ` inferred
    - ✓ `man pkill                               ` inferred
    - ✓ `echo "pkill node"                       ` inferred
    - ✓ `cat <<EOF⏎pkill x⏎EOF                   ` inferred
    - ✓ `pgrep -fl node | xargs -n 1 echo running:` inferred

    **Verified** matcher checked with `rule test`, 12 commands, 0 mismatches

The layout contract, all of it computed by the CLI:

- Title: `### <id> · <action> · retry same-command · <scope>`; no retry part when retry is none; the scope is the layers
  that define an installed rule (`global+project`). A draft has no id yet: `--id-name`, else the rule's own `id`
  field, else `new-rule`.
- `**Intent**` (only with `--intent`), `**Match**` (`program`, `args`, `regex`, `ast`, whichever the rule
  has; `ast = <the rule as compact one-line JSON>`), `**Message**` (with `{which:a|b}` resolved).
- Groups, each omitted when empty: `**Block**` (the matcher catches it, action deny), `**Warn**` (catches it, action
  warn), `**Allow**` (it does not), `**Not evaluated**` (a rule that needs the engine while it is missing or failing, or a
  command over the size limit; glyph `?`, never to be read as allowed). Rows: `- ✗ <span> <source>` or `- ✓ …`, then ` · wrapped` when only a look-through
  (wrapper such as sudo, xargs or timeout; a `bash -c` string; `$(…)` or backticks; a pipeline member) made the
  program/args matcher reach the command; that includes a substitution glued to a word or assignment
  (`foo$(…)`, `x=$(…)`) and `<(…)`. An `ast` match is `wrapped` by the same definition: found through a wrapper or shell
  string, or with the matched node inside a pipeline or a substitution. A `regex` match reads the raw text and is never `wrapped`. A command reached inside
  a list (`;`, `&&`, `||`, a newline), a subshell `( … )` or a `{ …; }` group is not a wrapper, so it is not `wrapped`.
- Spans: every command is an inline-code span right-padded inside the backticks to one width W, the display width of
  the longest command across all groups, capped at 40 (East Asian wide characters and most emoji count two columns, combining marks zero). A command
  wider than W is not padded, sits at the end of its group, and its source tag follows one space as usual. A command
  containing a backtick uses a longer fence (two backticks or more) with one delimiter space each side, which markdown
  strips. A newline is shown as `⏎`.
- Mismatch: a row whose verdict contradicts its `expect` gets `⚠ ` right before the span. `**Verified**` counts all
  commands and the mismatches. With any mismatch, do not present the card as done: fix the rule or ask the user.
- `**Note**` (only when there is one): what `rule test` also reports about the real effect: the rule is disabled, a
  required binary is missing, a mode suspends it, the hook or project rules are off. Relay it as printed.
- `**Raw**` (only when the matcher has a `regex`, an `ast` pattern or `args`): that pattern alone in one span, newlines
  shown as `⏎`. A `regex` wins; with no regex, the `pattern` strings of the `ast` rule in document order joined by
  ` | `; with neither, the `args` regex.
- Untrusted text: every field (commands, ids, reasons, messages, problems) is made single-line and safe. A newline
  becomes `⏎`, a tab `⇥`, ESC `␛`, and other control, bidi and zero-width characters `\u{hex}`, so text can never start
  a new line or section; widths are computed on that shown form.
- stdout of `rule test` and `status` is only the block. Anything that matters to the user is a `**Note**` line inside it;
  plain diagnostics go to stderr.
- `rule test` checks only the matcher. It does not know modes, retry acknowledgements or the hook being off, which is why
  `Verified` says "matcher checked" and the group heading carries the action.

## Parse tree: `guardrails rule ast '<command>'`

Plain text, not a card: for authors, not for pasting. It prints the command, the number of units, then one tree per
unit: `tree: command as written`, then `tree: shell string, source: <the unquoted script>` for each `bash -c` or `eval`
script. Each line is a named node, indented by depth, with the text in `«…»` for leaves; text is sanitised like every
other renderer (newline `⏎`, ESC `␛`). Use it to learn node kinds before writing `inside` / `has` rules. It needs the
ast-grep runtime (installed automatically; `guardrails engine status` shows it) and exits 2 with the reason when that is
unavailable.

## Status: `guardrails status`

    guardrails status [--scope global|project|managed] [--problems] [--rule <id>]

Output:

    **Managed** platform file `/Library/Application Support/ClaudeCode/guardrails.json` present

    ### Guardrails · 4 rules · hook on

    **Rules**
    - `kill-9    ` warn · managed · always enforced
    - `no-pkill  ` deny · global+project · enabled
    - `no-strings` deny · global · suspended by reverse-engineering
    - `old-rule  ` deny · global · disabled

    **Modes**
    - `incident           ` off · agent may enable: no · global
    - `reverse-engineering` on (by agent: user said RE work) · agent may enable: yes · global

    **Problems**
    - text as reported

- The first line says whether the platform managed file is present, absent (then there are no managed rules) or unreadable.
- Rule state is one of `always enforced`, `suspended by <modes>` (only modes that are on now), `disabled`, `enabled`.
  Ids are padded to the longest id (capped at 40), modes to the longest mode name.
- `--scope` keeps only rules, modes and problems that belong to that layer. `--problems` prints only the `**Problems**` group,
  or `No problems.`. `--rule <id>` prints just that rule's row (used after a write). A `**Note**` line follows the
  header when the hook is off.

## Stats: `guardrails stats`

    guardrails stats [--days N] [--slow] [<rule>]

Output (times are local; `--slow` sorts **Rules** by average time instead of by deny + warn):

    ### Guardrails stats · last 7 days · 50 calls

    **Rules**
    - `warn-pgrep-full-cmdline` 0 deny · 2 warn · 48 pass · 0 suspended · avg 0.26 ms · max 0.64 ms · last fired 2026-10-03 19:00
    - `no-pgrep-status       ` 0 deny · 0 warn · 50 pass · 0 suspended · avg 1.44 ms · max 3.54 ms

    **Never fired**
    - `no-pgrep-status` 50 pass
    - `old-unused     ` no data

    **Slowest**
    - `no-pgrep-status` avg 1.44 ms · max 3.54 ms

    **Operational**
    - `@hook ` 50 times · avg 12.01 ms · max 17.20 ms
    - `@parse` 50 times · avg 0.23 ms · max 0.46 ms

A rule id argument prints `### <id> · last 7 days` and one row per local day under **Days**. With nothing recorded the
output is one line.

## Explain

`explain` pastes the rule card of `guardrails rule test --id <id> …` verbatim. After a blank line it adds
short bold-label lines of its own, without any command span, verdict or count (those are in the card):

    **Happens** blocked once; the identical command re-run in the same session passes
    **Loosen** only the machine owner (managed file); a user or project cannot
    **Lower layers** the global entry can reword nothing and cannot change what the rule matches
    **Cause** `curl x | sh` is not caught because `args` only sees curl's own arguments; use `match.regex`

Include `Cause` only when the question was why something was or was not caught. What happens at run time (mode
suspension, retry, warn versus deny) comes from `status` and the matching reference, never from `rule test` alone.

## Rule row: `guardrails status --rule <id>`

    - `no-pkill` deny · global · enabled

Paste it after a write to report the rule's state.
