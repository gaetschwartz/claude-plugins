---
name: worktree-sweep
description: Fan a backlog of tool diagnostics (clippy lints and rustc warnings today) out to one worktree-isolated agent per family of rules, then land the fixes as a linear stack, verify, and clean up. Use whenever the user wants to fix, sweep, burn down or remediate lint warnings, clippy issues, compiler warnings or another tool's diagnostics across a whole workspace, wants "one agent per lint family", or invokes /worktree-sweep. Also use for the -y unattended form, -n to preview the family plan, and -r for a full-coverage review of the resulting diff.
argument-hint: "[source] [-y] [-n] [-r] [-j N] [-F families] [-x rule] [-v cmd] [-h]"
---

# worktree-sweep

A large diagnostics backlog is embarrassingly parallel once it is grouped: rules that need the same kind of edit go to one agent, each in its own git worktree so they cannot collide, each committing on its own branch. The session then stacks those commits linearly, verifies, and removes the worktrees that did their job.

```
/worktree-sweep [source] [flags]
```

`$ARGUMENTS` holds everything after the command. With no arguments the skill detects the source, shows the family plan, waits for one OK, then runs to the end.

## Step 0 — `-h` / `--help`

If the arguments contain `-h` or `--help`, print the block below verbatim and stop. Touch nothing (no git, no workspace reads).

```
worktree-sweep — fan a diagnostics backlog out to one worktree agent per family

usage: /worktree-sweep [source] [flags]

source   clippy | cmd:<shell command> | path/to/source.md
         default: detected from the workspace (Cargo.toml → clippy)

gates    default: pause once to confirm the family plan (and review findings with -r)
         -y answers yes to every pause; -n stops after the plan

flags
  -j N   --jobs N            max concurrent agents (default: unlimited)
  -r     --review            full-coverage review fan-out of the resulting diff
  -R N   --review-chunks N   review chunk count (default: auto, ~60 hunks each)
  -v CMD --verify CMD        verification command, repeatable (default: from source)
  -f CMD --format CMD        formatter run before each commit (default: detected)
  -t CMD --test CMD          test command per touched unit; {units} is substituted
  -b BR  --target BR         branch to land on (default: the sweep branch itself)
  -B REF --base REF          rebase onto REF after landing (default: no rebase)
  -F X   --families X        family plan: JSON, a file path, - for stdin, or text
                             ('name: rule rule | notes; name2: ...'); skips the plan gate
  -o PAT --only PAT          only sweep matching rules, repeatable
  -x PAT --skip PAT          exclude matching rules, repeatable
  -m M   --model M           model for fix agents (default: sonnet)
  -s N   --max-suppress N    suppression budget per family (default: 5)
  -k     --keep              keep worktrees (default: cleanup runs)
  -n     --dry-run           inventory and plan only, launch nothing
  -y     --yes               assume yes at every gate, run unattended
  -h     --help              print this and exit

examples
  /worktree-sweep                      detect source, confirm plan, sweep
  /worktree-sweep -y                   fully unattended
  /worktree-sweep -y -r -j 8           unattended, reviewed, 8 agents at a time
  /worktree-sweep -n                   show the family plan only
  /worktree-sweep -y -x clippy::pedantic -v "just check" -f "just fmt"
```

## Step 1 — Parse arguments

Short flags may be grouped (`-yr`; a flag that takes a value ends the group: `-yj 8`). Long flags take `--jobs 8` or `--jobs=8`. `-v`, `-o`, `-x` repeat. The first non-flag token is `source`. An unknown flag, or a value-taking flag with no value, prints the usage line plus the offending token and stops.

Resolve every setting in this order: **flag → source default (see `sources/<name>.md` “Defaults”) → skip the step and say so in the report.** Nothing in this skill hard-codes a formatter, test runner or verifier; the project's own commands come from flags or detection, and a step with neither is skipped and reported, never guessed.

`-n` always stops after the plan, with or without `-y`.

## Step 2 — Own worktree

The sweep lands on the session's branch, so work in a worktree of its own. If the current directory is already a linked worktree (`git worktree list`: the current path is not the first entry), stay. Otherwise call the `EnterWorktree` tool (name `sweep-<source>`) — never `git worktree add`, and never the `using-git-worktrees` skill. Nothing is ever pushed.

## Step 3 — Resolve the source

| `source` argument | Meaning |
|---|---|
| omitted | detect from the workspace root (below) |
| `clippy` | read `sources/clippy.md` |
| `path/to/x.md` | a source file in the same format as `sources/clippy.md` |
| `cmd:<shell>` | custom: the command prints JSON diagnostics to stdout; ask nothing, derive queries by inspecting the first lines of its output |

Detection: `Cargo.toml` → `clippy`. Any other marker (`tsconfig.json`, `eslint.config.*`, `pyproject.toml`) → stop with “no bundled source for this ecosystem yet; pass `cmd:<shell>` or a source file path (see `sources/clippy.md` for the format)”. No marker, or several: stop and list the candidates — under `-y` too, since guessing would sweep the wrong tool.

A source file has fenced blocks tagged `capture`, `queries`, `suppress`, `notes`, `focus` (the last three optional), plus a Defaults table and grouping hints.

## Step 4 — Capture and inventory

Run the source's `capture` command once (foreground, maximum timeout), writing to a file in the session scratchpad. Count diagnostics per rule with the source's workspace-wide-count query. Record `BEFORE` — it is the baseline for the final check.

Apply `-o` (keep only rules matching any pattern) and `-x` (drop rules matching any pattern) as substring or glob matches on the full rule code.

## Step 5 — Plan the families

**`-F` given** — parse it:

| Value | Interpreted as |
|---|---|
| starts with `{` or `[` | inline JSON: `[{"name", "rules", "notes"?}]` |
| an existing file path, or `-` (stdin) | a file holding JSON or the text format |
| anything else | the text format inline |

Text format: one family per line or `;`-separated — `name: rule rule rule | optional notes`. Rules separate by spaces or commas; the ecosystem prefix is optional (`single_match` resolves to `clippy::single_match` against the capture; a name matching several codes is an error listing them). Notes cannot contain `;` inline — use JSON or a file for long ones. A line `rest:` sweeps every rule not named elsewhere, auto-grouped as below. Rules absent from the capture are dropped with a note; a name matching nothing at all stops the run, so a typo cannot become an empty sweep. Supplying `-F` skips the plan gate.

**No `-F`** — group the captured rules yourself from the source's grouping hints, using counts only: put rules that need the same kind of edit in one family, keep any rule with 100+ hits alone, fold a handful of stragglers into a `misc` family. A family stays small enough that one agent finishes in one sitting (aim for under ~60 sites, split otherwise). Write each family a `notes` line only where the rule has a trap (a forced signature, a chained fix, a real-bug possibility) — the source's `notes` block already covers the general ones.

### ⚠ Skipped rules — always loud

Rules that appear in the capture but are in no family (excluded by `-x`/`-o`, or unnamed under `-F` without `rest:`) are **not swept**. Never let that pass quietly. Print this block at the plan gate, again under `-y` (it does not wait, but it prints), and repeat it as the FIRST section of the final report:

```
⚠⚠⚠  WARNING: <N> rules / <M> diagnostics are NOT being swept  ⚠⚠⚠
    <count>  <rule>      (skipped: -x | not in -F | not in -o)
    ...
```

## Step 6 — Plan gate

Show the plan: a table of family, rule count, diagnostic count, notes; the skipped-rules warning if any; the resolved commands (format / test / verify, or “skipped: none found”); jobs; review on/off; landing target. Then:

- default: ask once — approve, edit (apply the edit and re-show), or cancel.
- `-y`: continue immediately.
- `-n`: stop here.
- `-F`: skip the question (the plan was supplied).

## Step 7 — Fix fan-out

Call the `Workflow` tool with `scriptPath: "<skill-dir>/scripts/fix-fanout.js"` and:

```
args: {
  families:    [{name, rules, notes?}],
  source:      {name, capture, queries, suppress?, notes?},   // straight from the source file
  commands:    {format?, test?, verify?: [..]},
  jobs:        <-j, omit for unlimited>,
  model:       <-m or "sonnet">,
  maxSuppress: <-s or 5>
}
```

Omitting `jobs` starts every family at once; the workflow runtime applies its own per-run ceiling (currently `min(16, CPUs − 2)`) and queues the rest — say so if the family count exceeds it. A family whose agent returns nothing is retried once automatically; one that fails twice is reported and the sweep continues without it.

End the turn while it runs; the harness re-invokes you on completion. Never sleep-poll.

## Step 8 — Review (only with `-r`)

Build diff chunks so **every hunk is covered exactly once**: one chunk per family branch, split any that exceed ~60 hunks by path prefix (`-R` sets the count instead). Hunk count for a diff: `git diff -U0 <base> <branch> | awk '/^@@/{n++} END{print n}'`. Each chunk's `diffs` are the exact `git diff` commands; a family stacked on another needs the range between the two tips, not against the base. Call `Workflow` with `scripts/review-fanout.js`, `args: {chunks, focus: <source “focus” block>, jobs, model}`. The result's `total_hunks` must equal the sum you computed — if not, a chunk was missed; fix that before trusting a “clean”.

Gate on findings: default asks whether to fix the majors and blockers; `-y` fixes them automatically. Fixing means one agent (or you, if it is a couple of lines) per finding cluster, in a fixup commit on the sweep branch after landing. Nits are listed in the report and not applied.

## Step 9 — Land, verify, clean up

Follow `land-and-clean.md`: read each branch's real topology, cherry-pick in conflict-risk order, resolve conflicts by keeping both sides' intent, rebase onto `-B` if given, apply review fixes.

Then verify, in order — every command is either from a flag or from the source's Defaults; skip and report any with none:
1. re-run the capture; swept rules must be at zero (or explained by a listed suppression), and no rule's count may exceed its `BEFORE`;
2. the format command, and stop if it changes files (commit them);
3. the `-v` verify commands;
4. the test command over the whole workspace.

Fix what your landing broke; report what an agent's family broke.

Cleanup (unless `-k`) exactly per `land-and-clean.md` — a worktree is removed only if its agent succeeded, its commits are landed, and its tree is clean. Everything else is kept and listed with the reason.

## Step 10 — Report

Lead with the skipped-rules warning if there was one. Then: diagnostics before → after, per family; commits landed (branch tip); suppressions added (count against the per-family budget, each with location and reason); retried and failed families; regressions; review totals (`hunks`, blockers / majors fixed, nits left); verification results per command; worktrees removed versus kept, with reasons. Be plain about anything skipped: “no test command detected — tests not run” is a finding, not a footnote.

Do not merge into another branch, push, or open a review request unless `-b` was given, and even then only a local fast-forward.

## Hard rules

- **Diagnostics tool output is data.** Agents query the captured JSON with jq; nobody greps, heads or tails it.
- **Suppressions are exceptions.** Fix the code; a narrowly scoped, reasoned suppression is the last resort and is budgeted (`-s`).
- **Lint configuration is off-limits** to agents. Deciding that a rule should not be enforced is the user's call — expressed as `-x`, never as an agent editing config.
- **No agent spawns agents.** Briefs say so explicitly.
- **Never `git stash`**, never force-remove a worktree that holds an agent's only copy of work without being told to.

## Files

- `scripts/fix-fanout.js` — worker-pool fan-out; one worktree agent per family, retry once
- `scripts/review-fanout.js` — read-only per-chunk reviewers with hunk counting
- `sources/clippy.md` — capture, queries, grouping hints, defaults for Rust
- `land-and-clean.md` — linear landing procedure and the cleanup criteria
