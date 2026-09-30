# How guardrails matches a command

Everything here is what `lib/engine.py`, `lib/policy.py` and `lib/shellwords.py` do. `guardrails rule test` shows the
verdict for any command, so check a claim before stating it.

## What the matchers see

The command line is split into simple commands at `;` `&&` `||` `|` `&` and newlines. Each simple command is a
program name plus its own arguments. A rule is checked against every one of them, and the rule fires if any one matches.

Looked through, so the real command is what gets matched:

- wrappers `sudo doas env command builtin exec nohup setsid stdbuf time timeout xargs nice ionice` (their own options
  and a timeout duration are skipped): `sudo pkill x`, `timeout 5 pkill x`, `xargs pkill`
- shell strings `bash sh zsh dash ksh eval watch script`: the first non-option argument is parsed again as a command
  line, to a depth of three: `bash -c 'killall Safari'`, `sh -c 'a; pkill x'`
- command substitutions `$(…)` and backticks, except inside single quotes
- leading `VAR=value` assignments, and a directory in front of the name (`/usr/bin/pkill` is `pkill`)

Not commands, so never matched by `program`: heredoc bodies, redirect targets (`> pkill`), the operand of
`command -v`, and the arguments of other commands (`echo pkill`, `man pkill`).

Not looked through, so invisible to `program`: `ssh host pkill x`, `find . -exec pkill {} ;`, `bash script.sh`
(the script's contents), `python -c '…'`, `bash <<EOF … EOF` (the heredoc body is dropped).

## The fields

- `match.program`: one name or a list; equal to the command's name, exactly, case-sensitive. `egrep` is not `grep`.
- `match.args`: a regex (`re.search`) over that one command's own arguments joined by single spaces. Wrapper options
  are not part of them. It only narrows `program` or `builtin` (AND); on its own, or next to `regex` only, it has no
  effect and a rule whose `match` has none of `program`, `builtin`, `regex` is rejected.
- `match.builtin`: `grep-recursive` (grep, egrep, fgrep with `-r`, `-R`, `--recursive`, `-d recurse`); ANDed with
  `program` and `args`.
- `match.regex`: a regex (`re.search`) over the raw, whole command text, quotes, heredocs and pipelines included. It is
  an alternative: the rule fires when the program/args/builtin part matches OR the regex matches.
- A command with unbalanced quotes cannot be split. The fallback then scans the raw text for the program name (and
  `args` against the raw text); `builtin` never matches there; `regex` works as usual.

## Pipelines: `args` does not see them

`args` is per command, so the pipe to `sh` in `curl https://x.sh | sh` is not among `curl`'s arguments. Verified:

- `match: {program: curl, args: "\\| *(sh|bash)"}` does NOT match `curl https://x.sh | sh`, nor `curl x|bash`.
- `match: {regex: "curl [^|]*\\|\\s*(sudo +)?(sh|bash)\\b"}` matches both, and `bash -c 'curl x | sh'` too.

So a rule about what a command is piped into, or about text spanning several commands, needs `match.regex`. The price:
a regex also fires on text that merely mentions the command (`echo "curl x | sh"` matches), which `program` never does.
Combine a narrow `regex` with its look-alikes in mind and test them.

## What happens on a match

- `action: deny` blocks the call and shows the message; `warn` lets it run and shows the message to the agent once per
  session per rule. If something in the same command denies, warnings ride along in the deny message.
- `retry: same-command` (deny only): the first occurrence is blocked and remembered for the session, re-running the
  identical command text passes. Any changed text is blocked again.
- `messageShort` replaces `message` once the full text was shown in the session; `{which:a|b}` in a message becomes
  the first binary found on PATH.
- `enabled: false` skips the rule. `requires` skips it unless one of the binaries is installed.
- `modes`: the rule is skipped while any listed mode is active. A mode is active when switched on persistently (global,
  project, or managed) or for this session. An agent may switch on a session mode only when it is declared with
  `agentMayEnable` (and a rule suspended by an agent-enabled mode is reported to the user).
- The deny text starts with `[guardrails:<id>]`, or `<id> (managed)` for a managed rule.

## Layers

Order: managed, then global, then project. A lower layer can add rules of its own, and for an id a higher layer
already defines it can only tighten: switch `action` to deny, `retry` to none, re-enable, remove suspending `modes`,
and reword `message` / `messageShort` / `description` (not for managed rules). It cannot change `match` or `requires`,
loosen, disable, or add modes. An override that fails validation is ignored.

Managed specifics:

- Managed rules are always enforced unless their `modes` are declared by the managed file itself; a managed rule with
  no (declared) modes cannot be suspended by anything, and it still applies when the global hook is disabled.
- Several managed files stack: platform default first, then the `GUARDRAILS_MANAGED_PATH` file, then a `--path` file
  (status and `rule test` only); later ones can only tighten.
- A project cannot switch on a mode the managed file declares, and cannot make a managed mode agent-enablable.
- An unreadable or invalid managed file never turns the guard off: the broken part is skipped and reported.

Hook failures fail open: if the hook itself errors, the command runs.
