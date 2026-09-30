# Presentation conventions

Unfenced markdown only: no code fences (they render as flat one-colour blocks), no tables, no prose paragraphs.
Glyphs `✗` `✓` `◆` only where they carry meaning. Commands and ids go in inline-code spans, right-padded with spaces
inside the backticks to the widest entry of the list, so the trailing tags line up in one column. Count characters;
the same width applies to every group of one display.

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

    **Allow**
    - ✓ `kill 4242               ` yours
    - ✓ `pgrep -fl node          ` inferred
    - ✓ `man pkill               ` inferred
    - ✓ `echo "pkill node"       ` inferred
    - ✓ `pkill -0 node           ` you chose

    **Verified** with `rule test`, 9 commands, 0 mismatches

(Shown indented here only so this file can contain it; output it as plain markdown, without the indent.)

- Header: `### <id> · <action> · retry same-command · <scope>`; omit the retry part when retry is none. For a managed
  scope on a file that is not the platform default, show the file path on the line after the header.
- Group headings are infinitives: `Block` (action deny) or `Warn` (action warn) for the ✗ group, `Allow` for the ✓
  group. Omit an empty group.
- Source tags: `yours` (from the user's examples or description), `inferred` (you elaborated it and it was
  unambiguous), `you chose` (decided in an edge-case question). Append ` · wrapped` when the command reaches the rule
  through a wrapper (sudo, bash -c, xargs, timeout, `$(…)`, a pipeline).
- Every verdict shown (✗ or ✓) must come from `rule test`. Never show one you did not run. The `Verified` line states
  the number of commands tested and how many disagreed with the rule's intent; with a mismatch, fix the rule or the list
  first.
- A final `**Raw**` line with the rule JSON as one inline-code span appears only in the `new` confirmation step.

## Rule rows (status, and after a write)

One line per rule, no fence:

    - `no-pkill ` deny · retry · global+project · suspended by `incident`
    - `kill-9   ` warn · managed · always enforced
    - `old-rule ` deny · global · disabled

Padded id, action, `retry` when it is same-command, origin layers joined with `+` (highest first), then the state when
there is one: `always enforced`, `suspended by <modes>` (only modes that are active now), `disabled`, `hook off`.

## Status layout

    ### Guardrails · 4 rules · hook on

    **Managed** platform file `/Library/Application Support/ClaudeCode/guardrails.json` absent · override `/tmp/g.json` in use

    **Rules**
    - rule rows …

    **Modes**
    - `incident ` off · agent may enable: no · global
    - `reverse-engineering` on (session) · agent may enable: yes · global

    **Problems**
    - text as reported

- The `Managed` line: always say which managed files exist. When the platform default file is absent and an override
  (`GUARDRAILS_MANAGED_PATH` or `--path`) is used, say exactly that, because nothing else is enforcing at that level.
- Modes: on or off, then `agent may enable: yes|no`, then origins. No problems means leave the heading out.

## Explain layout

    ### no-pkill · deny · retry same-command · managed+global

    **Matches** program `pkill`, `killall`, including wrapped forms; not `pgrep`, `man pkill`, `echo pkill`
    **Happens** blocked once; the identical command re-run in the same session passes
    **Loosen** only the machine owner (managed file); a user or project cannot
    **Lower layers** global entry: rewords nothing, no effect on what it matches
    **Cause** `curl x | sh` passes because `args` only sees curl's own arguments; use `match.regex`

    **Verified** with `rule test`, 4 commands

    - ✗ `sudo pkill -f vite      ` blocked
    - ✓ `pgrep -fl node          ` passes

Bold labels, no paragraphs: each label gets one line. Include a `Cause` line only when the question was why something
was or was not caught. The `Verified` line and the example rows appear only for commands actually run through
`rule test`.
