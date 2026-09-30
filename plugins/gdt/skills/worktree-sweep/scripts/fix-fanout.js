export const meta = {
  name: 'worktree-sweep-fix',
  description: 'One worktree-isolated agent per diagnostic family, each fixing its family and committing on its own branch',
  phases: [{ title: 'Fix', detail: 'one worktree agent per family' }],
}

// args: {
//   families: [{name, rules: string[], notes?: string}],
//   source:   {name, capture, queries, suppress?, notes?},
//   commands?: {format?: string, test?: string, verify?: string[]},
//   jobs?: number,          // omit or 0 = every family at once
//   model?: string,         // default 'sonnet'
//   maxSuppress?: number,   // default 5
//   conventions?: string,   // appended verbatim to every brief
// }
//   source.capture  shell command that prints the FULL machine-readable diagnostics to stdout
//   source.queries  query cookbook; {{FILE}} = capture path, {{RULES}} = the family's rule names
//   source.suppress how this ecosystem silences one diagnostic (e.g. "#[expect(lint, reason = ...)]")

const families = args.families
const S = args.source
if (!Array.isArray(families) || !families.length) throw new Error('args.families must be a non-empty array')
if (!S || !S.name || !S.capture || !S.queries) throw new Error('args.source needs {name, capture, queries}')

const CMD = args.commands || {}
const MODEL = args.model || 'sonnet'
const MAX_SUPPRESS = args.maxSuppress ?? 5
const CONVENTIONS = args.conventions || ''

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    family: { type: 'string' },
    branch: { type: 'string' },
    commit: { type: 'string' },
    counts: {
      type: 'array',
      items: {
        type: 'object',
        properties: { rule: { type: 'string' }, before: { type: 'number' }, after: { type: 'number' } },
        required: ['rule', 'before', 'after'],
      },
    },
    regressions: { type: 'array', items: { type: 'string' } },
    suppressions: { type: 'array', items: { type: 'string' } },
    remaining: {
      type: 'array',
      items: {
        type: 'object',
        properties: { rule: { type: 'string' }, site: { type: 'string' }, reason: { type: 'string' } },
        required: ['rule', 'site', 'reason'],
      },
    },
    units_touched: { type: 'array', items: { type: 'string' } },
    tests: { type: 'string', enum: ['passed', 'failed', 'not_run'] },
    notes: { type: 'string' },
  },
  required: ['family', 'branch', 'commit', 'counts', 'regressions', 'remaining', 'units_touched', 'tests'],
}

function queries(f) {
  return S.queries.split('{{FILE}}').join('FILE').split('{{RULES}}').join(f.rules.join(' '))
}

function verifySteps() {
  const steps = []
  steps.push(
    'Re-capture the diagnostics to a SECOND file (same command). Your family\'s rule counts must be 0 (a suppression silences a diagnostic, so those count as 0 - list them in "suppressions"). No other rule\'s workspace-wide count may increase versus BEFORE; list any that did in "regressions" and fix them if they are yours to fix.',
  )
  steps.push(
    CMD.format
      ? `Run the formatter: \`${CMD.format}\`.`
      : 'No formatter command was given: do not run a formatter.',
  )
  steps.push(
    CMD.test
      ? `Run the tests: \`${CMD.test}\`. If the command contains {units}, replace it with the units you touched (crates/packages/paths); otherwise run it as written. Fix failures you caused.`
      : 'No test command was given: report tests as "not_run".',
  )
  for (const v of CMD.verify || []) steps.push(`Run \`${v}\` and fix anything it reports that you caused.`)
  steps.push(
    `Commit on your worktree branch: stage the changed files explicitly by path (never add-all / commit -a), conventional message \`refactor(lint): fix FAMILY ${S.name} diagnostics\` with FAMILY replaced by your family name (imperative, bulleted body listing rules and units). Do NOT push, merge, rebase or open review requests.`,
  )
  return steps.map((s, i) => `${i + 1}. ${s}`).join('\n')
}

function prompt(f, retry) {
  const rules = f.rules.join(' ')
  return `You are fixing one family of ${S.name} diagnostics in the workspace at your current directory, which is YOUR OWN git worktree. Be fast and efficient: prefer mechanical fixes, do not over-think, do not explore beyond what the diagnostics require.${retry ? '\n\nRETRY: a previous attempt at this family died before reporting. Ignore any stale state; capture fresh and start from the code as it is in YOUR worktree.' : ''}

FAMILY: ${f.name}
RULES: ${rules}

FAMILY-SPECIFIC GUIDANCE:
${f.notes || '(none)'}
${S.notes ? `\nSOURCE GUIDANCE (${S.name}):\n${S.notes}\n` : ''}
SUBAGENT POLICY: do not spawn subagents.

HYGIENE
- Work only inside your worktree (your cwd). Never touch the main checkout. Never start a dev server.
- Bash state does not persist between calls: create the capture file ONCE, note its literal path, and reuse that literal path in every later command (no shell variables).
- Builds and tests run in the FOREGROUND with the maximum timeout. Never background them; re-run on timeout.
- NEVER use grep, rg, head or tail (in Bash or via any search tool). Read code with the Read tool (offset/limit) or symbol-aware tools; filter diagnostics with jq.
- ALWAYS prefer jq over python for anything touching the diagnostics JSON. If a jq query fails twice, stop tunnel-visioning and switch to python for that task.
- Comments: default to ZERO. Never add explanatory comments, and never restate a suppression's reason in a comment.
- Suppressions (${S.suppress || 'the ecosystem\'s inline suppression'}) are EXCEPTIONS, used with great moderation and never as a shortcut past a hard case: only when the fix is impossible or would make the code worse (a signature forced by a trait, macro or framework, or an intentional pattern the tool cannot understand). Narrowest scope (the item or expression, never a module or project), mandatory reason, at a single canonical site. At most ${MAX_SUPPRESS} in this family - if you would exceed that, stop and fix properly. List every one in "suppressions" with its justification. Never edit lint configuration.
- Follow the project's own conventions (CLAUDE.md and rules files are already in your context). Breaking the project's internal API is fine; fix the callers instead of adding compatibility shims or re-exports.${CONVENTIONS ? '\n' + CONVENTIONS : ''}

STEP 1 - CAPTURE ONCE, QUERY MANY TIMES
Capture the FULL diagnostics to a file, then run every query as \`cat <file> | jq ...\`. Do not re-run the tool just to look at diagnostics again.
\`\`\`bash
mktemp -t sweep          # prints the path; use it literally below
${S.capture} > <that path>
\`\`\`
If a capture unexpectedly shows zero hits for your rules, the tool may be replaying a cache: invalidate it and re-capture. Record a workspace-wide BEFORE count per rule for the regression check in step 3.

Ready-made queries for your rules (replace FILE with the literal capture path):
\`\`\`bash
${queries(f)}
\`\`\`
Adapt them freely.

STEP 2 - FIX
Work the worklist. Use mechanical fixes wherever possible (a jq/python-driven rewrite from machine-applicable suggestions, or scripted edits from file:line:col), then hand-fix the rest. Read every file fully before editing it; earlier reads go stale after edits. Do not simplify away a requirement to escape a hard case: solve it, or record it under "remaining" with the precise reason.

STEP 3 - VERIFY
${verifySteps()}

STEP 4 - RETURN
You MUST finish by calling the StructuredOutput tool - a run that ends without it is treated as failed and its work is discarded. Fields: family, branch (the current branch name), commit (short sha), counts (before/after per family rule), regressions, suppressions (file:line plus justification), remaining (anything unfixed, with the reason), units_touched, tests, notes (one or two lines max).`
}

const width = args.jobs > 0 ? Math.min(args.jobs, families.length) : families.length
const queue = families.map((f, i) => ({ f, i }))
const results = []
const retried = []

async function runFamily(f) {
  const opts = { label: `fix:${f.name}`, phase: 'Fix', model: MODEL, isolation: 'worktree', schema: RESULT_SCHEMA }
  let r = await agent(prompt(f, false), opts)
  if (!r) {
    retried.push(f.name)
    log(`retry ${f.name}: first attempt returned nothing`)
    r = await agent(prompt(f, true), { ...opts, label: `fix:${f.name}:retry` })
  }
  return r
}

async function worker() {
  while (queue.length) {
    const { f, i } = queue.shift()
    log(`start ${f.name} (${f.rules.length} rules)`)
    const r = await runFamily(f)
    results[i] = r || { family: f.name, failed: true }
    log(`done ${f.name}: ${r ? `${r.branch} @ ${r.commit}, tests ${r.tests}` : 'FAILED after retry'}`)
  }
}

phase('Fix')
await parallel(Array.from({ length: width }, () => () => worker()))

const ok = results.filter((r) => r && !r.failed)
return {
  branches: ok.map((r) => ({ family: r.family, branch: r.branch, commit: r.commit, tests: r.tests, units: r.units_touched })),
  remaining: ok.flatMap((r) => (r.remaining || []).map((x) => ({ family: r.family, ...x }))),
  regressions: ok.flatMap((r) => (r.regressions || []).map((x) => ({ family: r.family, regression: x }))),
  suppressions: ok.flatMap((r) => (r.suppressions || []).map((x) => ({ family: r.family, at: x }))),
  retried,
  failed: results.filter((r) => !r || r.failed).map((r) => r && r.family),
}
