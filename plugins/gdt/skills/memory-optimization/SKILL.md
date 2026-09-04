---
name: memory-optimization
description: Fact-check, prune, merge and re-index a Claude Code long-term memory store (~/.claude/projects/<project>/memory/) using a fan-out of subagents that verify every claim against the live system. Use whenever the user says their memory files have grown too numerous or bloated, wants memories audited/pruned/consolidated/fact-checked/deduplicated/verified, suspects memories have gone stale or contradict reality, asks to clean up or reorganize MEMORY.md, or mentions memory rot, dangling [[links]], or "too many memory files". Also use when a memory store is merely suspected of drift after a big infrastructure change, and when the user wants only half the job — just fact-checking, or just pruning.
---

# Memory store optimization

A long-term memory store decays in two directions at once. Facts go stale as the
system they describe changes underneath them, and files multiply until recall
surfaces a dozen half-relevant fragments instead of one good answer. This skill runs
both repairs as one pipelined fan-out: verify against reality, then consolidate to
the smallest set that still answers every question.

The store is `~/.claude/projects/<project>/memory/` — one fact per file with YAML
frontmatter, plus `MEMORY.md`, a one-line-per-file index loaded into context every
session. That index is why bloat is expensive: every file is rent, paid on every
session forever.

If several project directories exist, the one to work on is the store whose
`MEMORY.md` is already in context. Confirm the path before touching anything; these
are the user's accumulated notes, not scratch files.

Below roughly 25 files a full fan-out is overkill — read them yourself, fix what is
wrong, and merge the obvious pairs. The pipeline earns its cost when there is more
material than one context can hold with care.

## The pipeline

`scripts/workflow.js` runs it all as one Workflow call. A planner agent groups the
files, then each batch flows through the stages independently — batch A can be
adjudicating while batch B is still fact-checking — so wall-clock is the slowest
single batch, not the sum of stages.

| Stage | Question the agent answers | Typical effect |
|---|---|---|
| **Plan** | Which files belong in a batch together? | One cheap agent, up front |
| **Fact-check** | Is every claim still true on the live system? | Many corrections, few deletions |
| **Consolidate** | What is the smallest set that answers every question? | Large file-count drop via merges |
| **Loss-check** | Did the merge silently drop a footgun? | Restores what consolidation lost |
| **Adjudicate** | Does each survivor earn a permanent slot? | Deletes husks; usually converges |

**Keep fact-checking and pruning separate.** An agent told to both verify claims and
prune aggressively will do the first and quietly skip the second — verification is
concrete and rewarding, pruning is a judgment call it can defer. Splitting them into
stages with one question each is the whole reason this works.

## Modes

`mode` selects how much of the pipeline runs. Default is `both`; run a single half
when the user asks for one, or when a full pass would be overkill.

| `mode` | Stages | Use when |
|---|---|---|
| `both` *(default)* | all four | a full audit — the store is both stale and bloated |
| `factcheck` | Fact-check only | after a big infrastructure change; the store is right-sized but reality moved |
| `prune` | Consolidate → Loss-check → Adjudicate | the store was verified recently and is merely too fragmented |

The briefs adapt: in `prune` mode the consolidator is told to spot-check anything
that reads as impossible rather than to assume a prior pass verified it, and
loss-check drops the caveat about corrected-as-false claims. Nothing to configure —
just set `mode`.

## Running it

### 1. Back up first, always

```bash
python3 scripts/memory_tools.py backup <memory-dir> --dest <session-scratchpad>
```

Writes a timestamped tarball plus an uncompressed *pristine* copy. `--dest` defaults
to a fresh temp dir; pass the session scratchpad when you have one. Agents delete
and rewrite files in place, and the pristine copy is what loss-check diffs against —
it is load-bearing, not just insurance. Note the path it prints.

### 2. Run the workflow

```
Workflow({
  scriptPath: "<skill-dir>/scripts/workflow.js",
  args: {
    dir: "/abs/path/to/memory",
    pristine: "/abs/path/from/step/1",
    mode: "both",
    model: "sonnet"
  }
})
```

Omit `batches` and a planner agent groups the files for you — it lists the directory
once, groups by subject domain, and the workflow then enforces exactly-once coverage
in code, sweeping anything the planner missed into a final batch. Pass `batches`
explicitly only when you want a specific grouping:

```
batches: [{key: "storage", files: ["project_zfs_pool.md", "..."]}, ...]
```

The grouping rule that matters: **anything that might merge must land in the same
batch**, because agents can only touch their own files — a merge that would cross a
batch boundary simply never happens.

`model` is optional and defaults to inheriting the session model. Sonnet is a good
fit: this is high-volume verification against a live system rather than deep
reasoning, and it lets you afford more batches. `plannerModel` overrides just the
planner, which runs at low effort since grouping by filename and description is not
hard.

`conventions` is optional and appended verbatim to every brief — use it for
host-specific shell rules the agents would otherwise trip over, e.g. *"Use `rg` not
`grep`, and `fd` not `find` — a hook rejects the others."* It goes into all briefs
identically, precisely so it cannot skew one agent against another.

Save the workflow's tool result to a file — the next steps read it.

### 3. Repair links, rebuild the index, validate

```bash
python3 scripts/memory_tools.py repair <memory-dir> --reports <result.json>
python3 scripts/memory_tools.py rebuild-index <memory-dir> --reports <result.json>
python3 scripts/memory_tools.py validate <memory-dir>
```

`repair` normalizes every `name:` slug, then repoints `[[links]]` whose targets were
merged away, using the merge map the workflow returns. Links pointing at something
that never existed are *reported, not deleted* — decide those by hand.
`rebuild-index` regenerates `MEMORY.md` and refuses to write if the reports and disk
disagree. `validate` is the final gate: frontmatter, slug/filename agreement,
dangling links, index coverage.

Run `validate` even after a `factcheck`-only run. Cross-batch link rot is invisible
to individual agents by construction, so it is nearly always present and nearly
always unnoticed.

Pass `--keep <literal>` to both `repair` and `validate` for `[[strings]]` that are
config syntax rather than links.

## Hard rules for the agents

These live in the briefs inside `workflow.js`. Change them there, not here.

- **No agent touches `MEMORY.md`.** A dozen agents appending to one index file
  corrupt it. The index is rebuilt deterministically from their reports instead.
- **Strictly read-only outside the memory directory.** Verifying a claim means
  reading configs, unit files and APIs — never restarting a service, editing a
  config, or killing a process. An audit that changes the system it audits is a bug.
- **Deleting for staleness requires positive evidence**, not an absent memory of the
  thing existing.
- **The pristine copy predates the fact-check**, so a claim missing from a
  consolidated file may have been removed *because it was proven false*. Loss-check
  verifies against the live system before restoring anything.

## Knowing when to stop

Watch the deletion count. When a pass deletes almost nothing and every surviving file
comes back with a written justification naming the lookup it answers, the store has
converged — independent agents each concluded the remainder pays rent. Running
another pass past that point produces churn, not compression: files get rewritten,
prose gets reshuffled, and the risk of losing a footgun rises with every rewrite
while the file count barely moves.

Expect the file count to fall by roughly a third and the line count by more. Most of
the reduction comes from **merges, not deletions** — a store that has been curated at
all is mostly real content in too many files, not junk.

## Reporting back

Lead with the numbers — files before/after, lines before/after, corrections made —
then the corrections that matter. A stale memory usually means something on the
system is quietly broken: a dataset with no snapshot policy, a probe pointing at a
decommissioned IP, a script path orphaned by a refactor. The workflow flags these as
`systemIsBroken` and surfaces them in `stats.systemIssues`. They are worth more to
the user than the pruning was, so name them individually rather than burying them in
a count.

Be straight about the pruning result. If it converged at a third rather than the
drastic cut the user pictured, say so and say why — independent passes agreeing is
evidence about the store, not timidity. Do not pad the deletion count to look
decisive.

## Files

- `scripts/workflow.js` — planner + four stages, with all agent briefs
- `scripts/memory_tools.py` — `backup`, `survey`, `repair`, `rebuild-index`, `validate`
  (`survey` is for eyeballing the store yourself; the planner does its own listing)
