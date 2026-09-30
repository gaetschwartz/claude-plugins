# Presentation conventions

Unfenced markdown only: no code fences (they render as flat one-colour blocks), no tables, no prose paragraphs.
Glyphs `✗` and `✓` only where they carry meaning. The examples below are indented four spaces only so this file can
show them; output them as plain markdown, without the indent.

## Spans and padding

- Commands and ids go in inline-code spans, right-padded with spaces inside the backticks so the tags after them line up
  in one column.
- One width per display: the length of the longest command across every group of the display (Block/Warn and Allow
  together). Ids are padded separately per list: rules to the longest rule id, modes to the longest mode name.
- Count characters exactly; a command that is exactly the widest gets no padding.
- Newlines in a command are shown as `⏎` (one character) and the command stays on one line.
- Text that contains a backtick uses a longer fence: one more backtick than the longest backtick run in the text, with a
  single space after the opening and before the closing fence (markdown strips exactly that one space). For such a
  span, pad the text itself to the display width, then add the delimiter spaces.

## Rule card (new, edit re-verification, explain)

    ### no-pkill · deny · retry same-command · global

    **Intent** stop killing processes by name, suggest `kill <pid>`
    **Match** program = `pkill`, `killall`
    **Message** Killing by name can hit the wrong process. Find the PID with `pgrep -fl <name>`, then `kill <pid>`.

    **Block**
    - ✗ `pkill node              ` yours
    - ✗ `sudo pkill -f vite      ` inferred · wrapped
    - ✗ `bash -c 'killall Safari'` inferred · wrapped
    - ✗ `killall Safari          ` inferred
    - ✗ `` echo `pkill x`           `` inferred

    **Allow**
    - ✓ `kill 4242               ` yours
    - ✓ `pgrep -fl node          ` inferred
    - ✓ `man pkill               ` inferred
    - ✓ `echo "pkill node"       ` inferred
    - ✓ `pkill -0 node           ` you chose
    - ✓ `cat <<EOF⏎pkill x⏎EOF   ` inferred

    **Verified** matcher checked with `rule test`, 11 commands, 0 mismatches
    **Raw** `{"match":{"program":["pkill","killall"]},"action":"deny","retry":"same-command","message":"Killing by name can hit the wrong process. Find the PID with pgrep -fl <name>, then kill <pid>."}`

- Header: `### <id> · <action> · retry same-command · <scope>`; omit the retry part when retry is none. For a managed scope
  on a file that is not the platform default, show the file path on the line after the header.
- Group headings are infinitives: `Block` (action deny) or `Warn` (action warn) for the ✗ group, `Allow` for the ✓
  group. Omit an empty group.
- Source tags: `yours` (from the user's examples or description), `inferred` (you elaborated it and it was
  unambiguous), `you chose` (decided in an edge-case question). Append ` · wrapped` when the command reaches the rule
  through a wrapper (sudo, bash -c, xargs, timeout, `$(…)`, a pipeline).
- `rule test` checks only the matcher: match or no match. It does not know about active modes, retry
  acknowledgements, the hook being off, or `warn` versus `deny`. So ✗ means "the matcher catches it", and the group
  heading carries the action. Worded that way, every ✗ or ✓ shown must come from `rule test`; never show one you did not
  run.
- The `Verified` line reads "matcher checked with `rule test`", gives the number of commands and how many disagreed
  with the rule's intent (fix the rule or the list first). When `rule test` printed `note:` lines (rule disabled,
  required binary missing, a mode that suspends it, hook or project rules off), repeat each one on its own line right
  below `Verified`, worded as "would not act: …".
- `**Raw**`: the rule JSON on one line in one span (use the longer fence when it contains a backtick). Only in the
  `new` confirmation step.

## Rule rows (status, and after a write)

    - `no-pkill` deny · retry · global+project · suspended by `incident`
    - `kill-9  ` warn · managed · always enforced
    - `old-rule` deny · global · disabled

Padded id, action, `retry` when it is same-command, origin layers joined with `+` (highest first), then the state when
there is one: `always enforced`, `suspended by <modes>` (only modes that are active now), `disabled`, `hook off`.

## Status layout

    ### Guardrails · 3 rules · hook on

    **Managed** platform file `/Library/Application Support/ClaudeCode/guardrails.json` absent · override `/tmp/g.json` in use

    **Rules**
    - rule rows …

    **Modes**
    - `incident           ` off · agent may enable: no · global
    - `reverse-engineering` on · agent may enable: yes · global

    **Problems**
    - text as reported

- The `Managed` line: always say which managed files exist. When the platform default file is absent and an override
  (`GUARDRAILS_MANAGED_PATH` or `--path`) is used, say exactly that, because nothing else is enforcing at that level.
- Modes: on or off (`on` when the CLI says `ACTIVE`; add the CLI's `(by …)` text if present), then
  `agent may enable: yes|no`, then origins. No problems means leave the heading out.

## Explain layout

    ### no-pkill · deny · retry same-command · managed+global

    **Matches** program `pkill`, `killall`, including wrapped forms; not `pgrep`, `man pkill`, `echo pkill`
    **Happens** blocked once; the identical command re-run in the same session passes
    **Loosen** only the machine owner (managed file); a user or project cannot
    **Lower layers** the global entry can reword nothing and cannot change what the rule matches
    **Cause** `curl x | sh` is not caught because `args` only sees curl's own arguments; use `match.regex`

    **Verified** matcher checked with `rule test`, 2 commands

    - ✗ `sudo pkill -f vite` caught
    - ✓ `pgrep -fl node    ` not caught

Bold labels, no paragraphs: each label gets one line. Include a `Cause` line only when the question was why something
was or was not caught. The `Verified` line and the example rows appear only for commands actually run through
`rule test`; relay `rule test`'s `note:` lines under `Verified` as in the rule card. What happens at run time (mode
suspension, retry, warn versus deny) comes from `status` and the matching reference, never from `rule test` alone.
