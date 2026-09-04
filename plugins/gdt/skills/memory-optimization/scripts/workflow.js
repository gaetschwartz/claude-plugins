export const meta = {
  name: 'memory-optimization',
  description: 'Plan batches, then fact-check and/or consolidate a Claude Code memory store',
  phases: [
    { title: 'Plan', detail: 'group files into batches by subject domain' },
    { title: 'Fact-check', detail: 'verify every claim against the live system' },
    { title: 'Consolidate', detail: 'merge to the smallest set that answers every question' },
    { title: 'Loss-check', detail: 'diff against the pristine copy, restore dropped footguns' },
    { title: 'Adjudicate', detail: 'does each survivor earn a permanent slot?' },
  ],
}

// args: {dir, pristine?, mode?, batches?, model?, plannerModel?, conventions?}
//   mode: 'both' (default) | 'factcheck' | 'prune'
//   batches: omit to have a planner agent group the files for you
const DIR = args.dir
const PRISTINE = args.pristine
const MODE = args.mode || 'both'
const MODEL = args.model
const PLANNER_MODEL = args.plannerModel || MODEL
const CONVENTIONS = args.conventions || ''

if (!DIR) throw new Error('args.dir is required')
if (!['both', 'factcheck', 'prune'].includes(MODE)) {
  throw new Error(`args.mode must be 'both', 'factcheck' or 'prune' (got ${JSON.stringify(MODE)})`)
}
const doFact = MODE === 'both' || MODE === 'factcheck'
const doPrune = MODE === 'both' || MODE === 'prune'
if (doPrune && !PRISTINE) {
  throw new Error("args.pristine is required whenever pruning — loss-check diffs against it")
}

const opts = (label, phase, model, effort) => {
  const o = { label, phase, effort: effort || 'high' }
  if (model) o.model = model
  return o
}

// ---------------------------------------------------------------- shared text

const FORMAT = `## Memory file format

\`\`\`markdown
---
name: <kebab-case slug matching the filename>
description: <one line; the words a future session's task would contain>
metadata:
  type: user | feedback | project | reference
---

<the fact. For feedback/project, include **Why:** and **How to apply:**. Link related memories with [[other-slug]].>
\`\`\`

Type meanings — \`user\`: who the owner is. \`feedback\`: guidance on how Claude should
work, with the why. \`project\`: ongoing work, goals or constraints not derivable from
code or config; dates must be absolute. \`reference\`: durable external facts and pointers.`

const RULES = `## Hard constraints

- Touch nothing outside your assigned files. Do NOT touch \`MEMORY.md\` — the
  orchestrator rebuilds it deterministically from your report. A dozen agents
  appending to one index file corrupt it.
- Strictly read-only on the rest of the system. Verifying a claim means reading
  configs, unit files, package state and local APIs — never restarting or enabling a
  service, editing a config, killing a process, or writing anywhere outside \`${DIR}\`.
  An audit that changes what it audits is a bug.
- Evidence over recollection. Anything you assert about the current state of the
  system, you checked.${CONVENTIONS ? '\n' + CONVENTIONS.split('\n').filter(l => l.trim()).map(l => '- ' + l.trim().replace(/^[-*]\s*/, '')).join('\n') : ''}`

const REPORT_TAIL = `Return the structured report. \`finalFiles\` must list every file that exists when you
are done, each with its \`MEMORY.md\` index line in the form
\`- [Title](file.md) — hook\`, where the hook is a terse fragment (not a sentence)
capturing what makes the memory worth opening.`

const filelist = files => files.map(f => `- ${f}`).join('\n')

// ---------------------------------------------------------------- schemas

const FILE_ENTRY = {
  type: 'object',
  required: ['file', 'absorbed', 'indexLine', 'section'],
  properties: {
    file: { type: 'string', description: 'Final filename after any rename or merge' },
    absorbed: { type: 'array', items: { type: 'string' }, description: 'Assigned files folded into this one. Empty if it stands alone.' },
    justification: { type: 'string', description: 'The lookup this file answers, and what a session would get wrong without it.' },
    indexLine: { type: 'string' },
    section: { type: 'string', enum: ['Environment', 'Services', 'User Preferences', 'Claude Code authoring'] },
  },
}

const REMOVED_ENTRY = {
  type: 'object',
  required: ['file', 'disposition', 'reason'],
  properties: {
    file: { type: 'string' },
    disposition: { type: 'string', enum: ['deleted', 'merged'] },
    reason: { type: 'string' },
  },
}

const PLAN_SCHEMA = {
  type: 'object',
  required: ['allFiles', 'batches'],
  properties: {
    allFiles: { type: 'array', items: { type: 'string' }, description: 'Every .md file in the directory except MEMORY.md, exactly as listed.' },
    batches: {
      type: 'array',
      items: {
        type: 'object',
        required: ['key', 'files'],
        properties: {
          key: { type: 'string', description: 'Short kebab-case domain label, e.g. "storage-backup".' },
          files: { type: 'array', items: { type: 'string' } },
        },
      },
    },
  },
}

const FACTCHECK_SCHEMA = {
  type: 'object',
  required: ['finalFiles', 'removed', 'corrections', 'notes'],
  properties: {
    finalFiles: { type: 'array', items: FILE_ENTRY },
    removed: { type: 'array', items: REMOVED_ENTRY },
    corrections: {
      type: 'array',
      description: 'Every factual error found and fixed, one entry each.',
      items: {
        type: 'object',
        required: ['file', 'wasClaimed', 'actual'],
        properties: {
          file: { type: 'string' },
          wasClaimed: { type: 'string' },
          actual: { type: 'string', description: 'What the live system shows, and how you checked.' },
          systemIsBroken: { type: 'boolean', description: 'True if the drift means something on the system is silently broken, not merely that the memory was stale.' },
        },
      },
    },
    notes: { type: 'string' },
  },
}

const PRUNE_SCHEMA = {
  type: 'object',
  required: ['finalFiles', 'removed', 'notes'],
  properties: {
    finalFiles: { type: 'array', items: FILE_ENTRY },
    removed: { type: 'array', items: REMOVED_ENTRY },
    notes: { type: 'string' },
  },
}

const LOSS_SCHEMA = {
  type: 'object',
  required: ['losses', 'verdict'],
  properties: {
    losses: {
      type: 'array',
      items: {
        type: 'object',
        required: ['fact', 'sourceFile', 'severity', 'restored'],
        properties: {
          fact: { type: 'string' },
          sourceFile: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'useful', 'trivial'] },
          restored: { type: 'boolean' },
        },
      },
    },
    verdict: { type: 'string' },
  },
}

// ---------------------------------------------------------------- briefs

function plannerBrief() {
  return `You are grouping the files of a Claude Code memory store into work batches. Other
agents will each be handed one batch to audit and consolidate.

Directory: \`${DIR}\`

## Step 1 — list

Run exactly this to get every file with its size and description in one pass:

\`\`\`bash
for f in "${DIR}"/*.md; do
  b=$(basename "$f"); [ "$b" = MEMORY.md ] && continue
  printf '%s\\t%s\\t%s\\n' "$b" "$(wc -l < "$f")" "$(sed -n 's/^description: *//p' "$f" | head -1)"
done
\`\`\`

Report every filename you see in \`allFiles\`, verbatim, excluding \`MEMORY.md\`.

## Step 2 — group

Group them into batches by **subject domain** — the service, subsystem or topic each
file is about. Judge by filename and description; you do not need to read the bodies.

Two rules decide the grouping, and the first matters far more than tidiness:

- **Anything that might merge must land in the same batch.** Agents can only touch
  files in their own batch, so a merge that would need to cross a batch boundary
  simply never happens — that content stays duplicated forever. When two files might
  plausibly cover the same subject, put them together. Err toward grouping.
- **6-12 files per batch.** Below that you waste an agent; above it the agent starts
  skimming. Stretch to 14 if it keeps an obviously-related family intact — keeping a
  family whole beats hitting the size target.

Every file must appear in exactly one batch. Give each batch a short kebab-case
domain label, e.g. \`storage-backup\`, \`home-assistant\`, \`download-clients\`.

Do not edit any file. This is a read-only planning task.

Return the structured report.`
}

function factcheckBrief(files) {
  return `You are auditing a slice of a personal long-term memory store used by Claude Code.
Each file records one durable fact about the user's system, and the store is loaded
by description-matching at the start of every session.

Your job in this pass is ONE thing: **establish what is still true.**${doPrune ? `
Consolidation and pruning happen in later passes by other agents — do not do their
work. If a file is bloated but accurate, leave it bloated.` : ''}

## Your assigned files

Directory: \`${DIR}\`
Files (these and ONLY these are yours to touch):
${filelist(files)}

${FORMAT}

## Method

Read every assigned file in full. Then, for every concrete claim — paths, ports,
unit and container names, image tags, versions, config keys, IP addresses, API and
workflow IDs, dataset names, schedules, and above all *whether a thing is still
enabled* — verify it against the live system. You have a shell. Read the config
file, query the local API, list the units, inspect the container. Check that a
referenced file still exists and still says what the memory claims.

Then fix what you found:

- Correct every false claim to what the system actually shows.
- Where a value has drifted and will drift again (a device letter, a public IP, a
  \`:latest\` image's current version), do not re-pin the new value — generalize the
  claim so it stops going stale, or drop it if it carried no reasoning.
- Convert relative dates ("last week", "recently") to absolute ones.
- Fix \`[[slug]]\` links whose targets do not resolve, within your own batch. Note
  suspected dangling links pointing outside it.
- Tighten \`description:\` so recall can actually match on it.

Delete a file only when verification proves its subject is simply gone, and it
carried no lesson that outlives the thing. Deleting for staleness requires positive
evidence, never an absent memory of the thing existing.

**Flag drift that means something is broken.** A memory saying a probe watches
\`10.0.0.1\` when the gateway moved years ago is not just a stale note — it means the
probe has been dead ever since. When a correction reveals that kind of silent
breakage, set \`systemIsBroken\` on it. Those findings are often worth more than the
audit itself.

${RULES}

${REPORT_TAIL}`
}

function consolidateBrief(files) {
  return `You are consolidating a slice of a personal long-term memory store used by Claude Code.
${doFact ? `
A prior pass already verified every claim in these files against the live system and
corrected what was wrong. **Do not redo that work.** Assume the content is accurate
unless something reads as internally contradictory — then spot-check just that claim.
` : `
Treat the content as accurate unless something reads as internally contradictory or
obviously impossible — then spot-check that claim against the live system before
carrying it forward.
`}
Your job this pass is ONE thing: **reduce the number of files.**

## Your assigned files

Directory: \`${DIR}\`
Files (these and ONLY these are yours to touch):
${filelist(files)}

${FORMAT}

## The organising principle

**One file per subject a future session would actually look up — not one file per
incident, per service, or per debugging session.**

That is the whole test. Memories are recalled by matching \`description:\` against the
task at hand. Five files that all surface for the same task should be one file. A
file that would only ever surface alongside another should not be its own file.

Group your assigned files by *the question they answer*, then write one file per
group:

- Pick the best existing filename in the group and rewrite it in place, or create a
  new \`<type>_<topic>.md\` when no existing name fits the merged subject.
- Rewrite as a single coherent memory. Do not concatenate. A reader must not be able
  to tell it was assembled from several files.
- Carry forward every durable fact, footgun, exact path, config key and ID — and
  above all every **why**. Those are the entire value of the store.
- Drop freely: narrative of what was tried, dated progress notes, resolved-incident
  chronology, restated command output, and prose padding.
- Delete the absorbed files.

A slice of N files usually collapses to roughly N/3. A slice that comes back near
its original count has not done the job — but do not hit a number by deleting
content. The count falls because files merge, not because facts vanish. Merged files
may be longer than the originals; judge by whether every line earns its place.

${RULES}

${REPORT_TAIL}`
}

function lossBrief(files, result) {
  const finals = (result.finalFiles || []).map(f => `- ${f.file}`).join('\n') || '(none reported)'
  return `You are the loss-check on a memory-consolidation pass. Another agent just merged a
slice of a personal memory store down to fewer files. Your job is to catch durable
knowledge that got dropped in the merge, and put it back.

Pristine pre-consolidation copies: \`${PRISTINE}/\`
Live consolidated store: \`${DIR}/\`

The slice originally consisted of these files — read them in the PRISTINE directory:
${filelist(files)}

It now consists of:
${finals}

## Method

Read every pristine file, then every surviving live file. For each pristine file,
walk its concrete content and ask of each item: is this preserved somewhere in the
live slice, or deliberately and correctly dropped?

Dropping is **correct** for narrative of what was tried, dated chronology,
resolved-incident storytelling, restated command output, and facts rediscoverable
from the system in seconds with no reasoning attached. The owner wants those gone.
Do not flag them.

Dropping is a **loss** for a footgun or failure mode, a non-obvious causal
explanation (the *why* behind a setting), an exact path/key/ID that would take real
digging to recover, a "never do X" rule, a hard-won constraint, or a
decision-with-rationale that would otherwise be relitigated.
${doFact ? `
**Critical caveat:** the pristine copies predate a fact-checking pass. A pristine
claim absent from the live file may have been removed *because it was proven false*,
not because it was lost. Before flagging anything, verify the claim against the live
system. If the pristine claim is false, it is not a loss — do not restore it.
` : `
Before flagging anything, verify the claim against the live system. A pristine claim
that is no longer true is not worth restoring.
`}
For every genuine loss of severity \`critical\` or \`useful\`, write the fact back into
the most appropriate surviving file, in that file's voice, as tightly as possible,
and set \`restored: true\`. Do not restore \`trivial\` ones, and never resurrect a
deleted file — fold the fact into a survivor instead.

${RULES}
- Never modify the pristine backup. It is the reference copy.

Return the structured report.`
}

function adjudicateBrief(files) {
  return `You are adjudicating a slice of a personal long-term memory store used by Claude Code.

Prior passes already ${doFact ? 'verified every claim against the live system and merged related files' : 'merged related files'}.
**Do not redo that work.** Assume the content is accurate; spot-check only a claim
that reads as internally contradictory.

Those passes corrected and merged, but deleted almost nothing. That is the gap you
are here to close.

## The question

For each assigned file, answer one question honestly:

> **Does this file earn a permanent slot in a memory store the owner has to live with forever?**

The store is loaded by description-matching at the start of every session. Every
file is rent, paid forever. A file pays rent only if a future session, working on a
real task, would get something **wrong or slow** without it.

## Your assigned files

Directory: \`${DIR}\`
Files (these and ONLY these are yours to touch):
${filelist(files)}

${FORMAT}

## Outcomes

**DELETE** — the default for anything that does not clearly pay rent. Deletion is
expected here, not exceptional. Archetypes that should die:

- A design, plan or investigation where **nothing was ultimately built or changed**.
  A future session gains nothing but the temptation to relitigate it.
- A thing that is **disabled, removed, superseded or parked**, where the memory only
  records that it is gone. Absence is discoverable in seconds.
- A **completed one-off migration** whose end state is now simply how the system is.
  The end state is visible; the journey is not knowledge.
- A **setting whose value lives in a config file**, with no reasoning attached. The
  config cannot go stale; the memory can, and will.
- **Descriptive setup notes** — what a service is, its port, its data directory.
- Anything a competent session **would do correctly by default** without being told.
- A file whose real content is one sentence padded into thirty lines.

**Never delete a genuine footgun**: a failure mode with a non-obvious cause, a
"never do X" rule that cost real time to learn, a decision-with-rationale that would
otherwise be relitigated, a data-loss trap, or a workaround for an upstream bug.
Those are the reason the store exists. When a file is mostly padding wrapped around
one such nugget, do not delete it wholesale — extract the nugget into the most
appropriate surviving file, then delete the husk.

**MERGE** when two files would surface for the same task. Rewrite as one coherent
memory, then delete the absorbed file.

**KEEP** only when the file already pays rent and reads tightly. If you keep a file,
state in \`justification\` what a session would get wrong without it. If you cannot
name that, the answer is delete or merge.

Also trim within survivors: any file over ~50 lines must justify every line.

Verify against the live system before deleting something as dead or superseded.
Evidence, not assumption.

${RULES}

${REPORT_TAIL}`
}

// ---------------------------------------------------------------- plan batches

let BATCHES = args.batches
if (!BATCHES || !BATCHES.length) {
  phase('Plan')
  const plan = await agent(plannerBrief(),
    { ...opts('plan:batches', 'Plan', PLANNER_MODEL, 'low'), schema: PLAN_SCHEMA })
  if (!plan) throw new Error('batch planner returned nothing — pass args.batches explicitly')

  // Trust the planner's grouping, not its bookkeeping: enforce exactly-once coverage here.
  const all = plan.allFiles.filter(f => f && f !== 'MEMORY.md')
  const known = new Set(all)
  const seen = new Set()
  BATCHES = []
  for (const b of plan.batches || []) {
    const files = (b.files || []).filter(f => known.has(f) && !seen.has(f))
    files.forEach(f => seen.add(f))
    if (files.length) BATCHES.push({ key: b.key || `batch-${BATCHES.length + 1}`, files })
  }
  const missed = all.filter(f => !seen.has(f))
  if (missed.length) {
    log(`planner left ${missed.length} file(s) unassigned — sweeping them into an "unassigned" batch`)
    BATCHES.push({ key: 'unassigned', files: missed })
  }
  log(`planned ${BATCHES.length} batches over ${all.length} files: ${BATCHES.map(b => `${b.key}(${b.files.length})`).join(' ')}`)
}

// ---------------------------------------------------------------- run

const stages = []

if (doFact) {
  stages.push(st => st && agent(factcheckBrief(st.files),
    { ...opts(`factcheck:${st.key}`, 'Fact-check', MODEL), schema: FACTCHECK_SCHEMA })
    .then(r => r ? { ...st, factcheck: r, last: r, files: r.finalFiles.map(f => f.file) } : st))
}

if (doPrune) {
  stages.push(st => st && agent(consolidateBrief(st.files),
    { ...opts(`merge:${st.key}`, 'Consolidate', MODEL), schema: PRUNE_SCHEMA })
    .then(r => r ? { ...st, consolidate: r, last: r, files: r.finalFiles.map(f => f.file) } : st))

  stages.push(st => (st && st.consolidate)
    ? agent(lossBrief(st.original, st.consolidate),
        { ...opts(`losscheck:${st.key}`, 'Loss-check', MODEL), schema: LOSS_SCHEMA })
        .then(r => ({ ...st, lossCheck: r }))
    : st)

  stages.push(st => (st && st.consolidate)
    ? agent(adjudicateBrief(st.files),
        { ...opts(`adjudicate:${st.key}`, 'Adjudicate', MODEL), schema: PRUNE_SCHEMA })
        .then(r => r ? { ...st, adjudicate: r, last: r, files: r.finalFiles.map(f => f.file) } : st)
    : st)
}

const items = BATCHES.map(b => ({ key: b.key, files: b.files, original: b.files, last: null }))
const results = (await pipeline(items, ...stages)).filter(Boolean).filter(r => r.last)

const failed = BATCHES.filter(b => !results.some(r => r.key === b.key)).map(b => b.key)
if (failed.length) log(`INCOMPLETE batches (rerun these): ${failed.join(', ')}`)

// Merge map: absorbed file -> surviving file, across every stage that ran. Link repair needs it.
const mergeMap = {}
for (const r of results) {
  for (const stage of [r.factcheck, r.consolidate, r.adjudicate]) {
    for (const f of (stage && stage.finalFiles) || []) {
      for (const a of f.absorbed || []) mergeMap[a] = f.file
    }
  }
}
for (const k of Object.keys(mergeMap)) {  // collapse a -> b -> c into a -> c
  let hops = 0
  while (mergeMap[mergeMap[k]] && mergeMap[k] !== k && hops++ < 10) mergeMap[k] = mergeMap[mergeMap[k]]
}

const reports = results.map(r => ({
  batch: r.key,
  assigned: r.original,
  finalFiles: r.last.finalFiles,
  corrections: (r.factcheck && r.factcheck.corrections) || [],
  removed: [r.factcheck, r.consolidate, r.adjudicate]
    .flatMap(s => (s && s.removed) || []),
  losses: (r.lossCheck && r.lossCheck.losses) || [],
  notes: [r.factcheck, r.consolidate, r.adjudicate]
    .map(s => s && s.notes).filter(Boolean).join('\n\n'),
}))

const before = BATCHES.reduce((n, b) => n + b.files.length, 0)
const after = reports.reduce((n, r) => n + r.finalFiles.length, 0)
const corrections = reports.reduce((n, r) => n + r.corrections.length, 0)
const broken = reports.flatMap(r => r.corrections.filter(c => c.systemIsBroken))
const restored = reports.reduce((n, r) => n + r.losses.filter(l => l.restored).length, 0)
const deleted = reports.reduce((n, r) => n + r.removed.filter(x => x.disposition === 'deleted').length, 0)

log(`mode=${MODE} | ${before} -> ${after} files | ${corrections} corrections | ${deleted} deleted | ${restored} facts restored`)
if (broken.length) log(`${broken.length} correction(s) indicate something on the system is silently broken`)

return { mode: MODE, reports, mergeMap, failed, batches: BATCHES,
         stats: { before, after, corrections, deleted, restored, systemIssues: broken } }
