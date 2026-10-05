# The runtime: install, trust and failure behaviour

`ast-grep-py` ships one wheel per CPython version, so guardrails brings its own Python instead of using the host's. The
runtime lives in `${CLAUDE_PLUGIN_DATA}/runtime/<id>/` (about 140 MB on disk, 46 MB downloaded): a portable `uv` (`bin/`), a
managed CPython 3.13 (`python/`), a venv with the hash-pinned library (`venv/`) and a `marker.json` written last. `<id>` is
`lib/runtime-id`, a digest of `lib/runtime-manifest.json` and `lib/runtime-requirements.txt`, so a pin change installs a new
runtime. `runtime/<id>` is a symlink to the current build.

## Install: automatic, no user command

1. SessionStart runs `ensure` synchronously (timeout 120 s; first ever install about 3 to 10 s, later sessions about 0.1 s).
2. The PreToolUse hook, when the runtime is not ready, allows the command with a loud notice (once per session, repeated
   every 10 minutes while a failure persists) and starts a detached `ensure`.
3. Every `guardrails` CLI call runs at once on a ready runtime and otherwise ensures first, in the foreground.

**While the runtime is not ready, no rule is enforced**, whole-text regex rules and managed rules included, because every rule runs
on the managed Python. The window is the first seconds of the first session, or until an install works after a failure. The
notice says so.

Limits: 10 s network timeout, 60 s budget per install. A failed attempt is stamped, and the next automatic attempt is 10
minutes later, then 1 hour, then 6 hours (a success resets it); while a stamp is fresh SessionStart and the hook do not
try, they say when the next attempt is. `guardrails engine ensure --retry-now` ignores the wait. Notices carry one fixed
phrase per failure class (DNS, connection, timeout, TLS, HTTP status, hash mismatch, disk, tool failure, library crash),
never text from the network, the environment or the repository; the raw detail goes to `runtime/install.log`.

An install builds in a fresh directory next to the link, self-tests it (the library must import and match a pipeline),
swaps the link with one atomic rename and only then removes the old build: a failed or interrupted install never touches
the runtime in use. Installs hold a `flock` (the OS releases it if the installer dies). Cleanup runs only inside an
install, under that lock: builds nobody points at go at once, runtimes of other pins once they are 30 days old (two plugin
versions sharing a data dir never delete each other's runtime; nothing is deleted on the hot path).

Platforms: macOS arm64 and x86_64, Linux glibc 2.28+ x86_64 and aarch64. Linux musl, older glibc, other architectures and
Windows get an "unsupported platform" notice and no install attempt.

## Trust model

The guard stops mistakes by an honest agent and by an agent steered by hostile repository content (a cloned repo's
`.claude/settings.json` can set environment variables). Other local users, same-user malware and anyone who can write the
plugin data dir or the global config are out of scope: they could edit the rules directly. Known gaps: a repository that
sets an absolute `CLAUDE_PLUGIN_DATA` chooses which runtime and session state the hook uses, and one that sets an absolute
`XDG_CONFIG_HOME` chooses which global config it reads. A repository's own `.claude/guardrails.json` is the project layer by
design: it can add rules and tighten others, never loosen a global or managed one.

Files: the runtime, `state.json` (per-session memory), `state.json.lock`, `config.lock`, `telemetry.db` and `notices/` live
in the data dir. Configuration does not: the global config is under the XDG config dir and the project config in
`<project>/.claude/guardrails.json` (see the README). The hook writes into the data dir only, never configuration.

Only `lib/bootstrap.py`, `lib/installer.py` and `lib/hostcli.py` run on the host's Python (3.9 or newer, standard
library only). The installer downloads the pinned `uv` wheel from the one `files.pythonhosted.org` URL in the manifest
(sha256 checked before the file is read, exactly one member unpacked), then runs that uv with a scrubbed environment
(HOME, LANG, TMPDIR, proxy and `SSL_CERT_*` variables; `PATH=/usr/bin:/bin`; `UV_*` pointing inside the runtime dir;
`UV_NO_CONFIG`; the runtime dir as cwd): `uv python install 3.13`, `uv venv`, `uv pip install --require-hashes
--only-binary :all: --no-deps`. A repository's `uv.toml`, `UV_*`, `PATH`, `PYTHONPATH` or `pip.conf` never influence what
runs. uv checks the Python distribution against the sha256 compiled into the uv binary, so the manifest pins uv, uv pins
Python and `--require-hashes` pins the library.

Network hosts: `pypi.org`, `files.pythonhosted.org`, `releases.astral.sh`, and `github.com` plus
`release-assets.githubusercontent.com` as the Python fallback. `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and `SSL_CERT_FILE/DIR`
are passed through; mirror and index variables are not.

The hook wrapper starts the runtime's Python, with `python -I`, only when `CLAUDE_PLUGIN_DATA` is an absolute path,
`marker.json` exists and no `broken` file is next to it (shell builtins only: nothing is spawned to decide); each `ensure`
also checks that the files the marker lists exist, and reinstalls otherwise. A relative `CLAUDE_PLUGIN_DATA` gets the notice
"the plugin data directory is not an absolute path" on every call and nothing runs from it. No notice, hook answer or
`engine status` text contains a path or any other text taken from the environment or the repository; the runtime and log
paths go to the stderr of `guardrails engine status` only. The project directory and the cwd never decide where the runtime
is.

The wrapper picks the host Python from fixed absolute locations first, then `PATH` entries that are absolute and outside
the project and cwd; each candidate is smoke-tested, and a broken one is reported (its path on stderr), never skipped
silently. A hook Python that dies is reported ("failed to run (exit N)").

## When the engine fails

There is no degraded parsing and no rule runs outside the engine; the hook allows the command and says so loudly.

- **Engine failure** (import error, failed self-test, an unexpected error in the checker): that call is allowed with a
  warning that names the reason, in both channels, repeated at most every 10 minutes while it lasts. Managed deny rules fail
  open too; the warning and `status --problems` name them.
- **A crash of the library**: the checker runs a health probe on a trivial command in a fresh child. Probe passes: that
  command crashes the parser and is denied alone ("this command crashes the parser"). Probe crashes or answers wrongly: the
  library is broken, the command is allowed with a loud notice, the runtime is marked broken and rebuilt in the background
  (same backoff as an install; the old build stays until the new one is swapped in). Probe silent for 3 s: retried once with
  a longer deadline (both bounded by the hook's 10 s budget minus 2 s headroom); still silent means unverified, not broken:
  allowed with "could not verify the matcher (timed out)", nothing is rebuilt. A checker that cannot be started is a loud
  allow that leaves the runtime alone.
- **A command the parser does not finish in 5 s is denied** ("command too complex to check"): everything that needs the
  parser, whole-text regex rules included, runs in a forked child killed at that deadline, because a native call into ast-grep holds
  the GIL and a hook that outlives its timeout lets the command through. A hit already found stands.
- **A rule that does not compile** (a `match` ast-grep rejects, or a regex Rust cannot compile) is skipped and named once
  per session. So is a rule with a field this version does not know, a malformed atom, or a `match` in the removed format
  (`program`, `ast`, a lone `regex`): the warning and `status --problems` name the rule and what is wrong, with the new
  form for a removed key.
- **A command over 256 KiB, nesting shell strings more than 8 deep, unpacking into more than 64 distinct strings or 256 KiB
  of script text, or unwrapping into more than 2048 variants or 512 KiB of variant text, is denied unparsed**: padding must
  never be a way past a rule. Only a deny rule that could not be judged causes the denial; warn-only rules are allowed with
  a warning.
- If the hook itself raises, it allows with a visible warning that no rule was applied.

`rule test` and `status --problems` say the same: a rule that needs the engine is reported as not evaluated, never as "no
match".

## Telemetry

Per-rule counters in `${CLAUDE_PLUGIN_DATA}/telemetry.db` (SQLite, mode 0600, local only: nothing is ever sent). One row per
rule id and hour: how many calls the rule `deny`-ed or `warn`-ed (it matched), let `pass` (evaluated, no match) or had
`suspended` by an active mode (it matched), plus the summed and the largest evaluation time in microseconds. A rule that was
not evaluated (disabled, `requires` missing, engine failure) writes no row. Rule ids are user text, so they are reduced to
at most 64 printable ASCII characters and never start with `@`. Rows older than 365 days are deleted at SessionStart.

Reserved `@` rows count what is not a rule: `@hook` (calls, and the time in the hook process; interpreter start-up is not
included), `@parse` (calls, and the time spent parsing and unwrapping, shared by all rules), and failures, counted in
`deny`: `@oversize`, `@complexity`, `@timeout`, `@crash`, `@engine-failure`.

**Never recorded:** the command or any part of it, arguments, paths, the project or working directory, session ids, host
names, message text, who enabled a mode. Only rule ids, counts, times and the hour.

The hook forks the checker first, then starts one thread that opens the database while the checker works; the counts are
written only after the answer is out, and the hook waits at most 20 ms for that write. A busy lock, a read-only or full disk,
a missing `sqlite3` or any error drops the sample silently and never changes an answer or an exit code; a file that is not a
database is renamed `telemetry.db.corrupt` and recreated. A hook whose runtime is not ready records nothing.

`guardrails stats [-d/--days N] [-s/--slow] [<rule>]` (default 7 days; the layout is in
[presentation.md](presentation.md#stats-guardrails-stats)); `guardrails stats --reset`, user only, deletes the database.

## Troubleshooting and kill switches

`guardrails engine status` needs no runtime and works offline: whether the runtime is ready (or why not), the pins, the
platform, the installed Python, whether an install is running and the last failure (phrase, count in a row, next automatic
attempt). For the user, if guardrails ever blocks everything: `claude plugin disable guardrails@<marketplace>`, or
`guardrails disable` from a terminal (global hook off; managed rules stay).
