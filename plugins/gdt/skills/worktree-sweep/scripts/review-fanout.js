export const meta = {
  name: 'worktree-sweep-review',
  description: 'Read-only reviewers over diff chunks of a sweep; every hunk of the combined diff is covered',
  phases: [{ title: 'Review', detail: 'one reviewer per diff chunk' }],
}

// args: {
//   chunks: [{name, diffs: string[], extra?: string}],   // diffs = exact git diff commands covering the chunk
//   focus?: string,        // source-specific things to verify (semantic-equivalence traps, etc.)
//   jobs?: number,         // omit or 0 = every chunk at once
//   model?: string,        // default 'sonnet'
//   conventions?: string,
// }

const chunks = args.chunks
if (!Array.isArray(chunks) || !chunks.length) throw new Error('args.chunks must be a non-empty array')
const MODEL = args.model || 'sonnet'

const FINDINGS = {
  type: 'object',
  properties: {
    chunk: { type: 'string' },
    verdict: { type: 'string', enum: ['clean', 'issues'] },
    hunks_reviewed: { type: 'number' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'number' },
          severity: { type: 'string', enum: ['blocker', 'major', 'nit'] },
          issue: { type: 'string' },
          evidence: { type: 'string' },
        },
        required: ['file', 'severity', 'issue', 'evidence'],
      },
    },
    notes: { type: 'string' },
  },
  required: ['chunk', 'verdict', 'hunks_reviewed', 'findings'],
}

function reviewPrompt(c) {
  return `You are reviewing part of an automated diagnostics-fix sweep in the workspace at your cwd (a git worktree; every sweep branch is reachable). READ-ONLY: change no files, run no builds or tests, commit nothing. Do not spawn subagents.

CHUNK: ${c.name}
Diff commands (run exactly these; together they cover your ENTIRE assignment):
${c.diffs.join('\n')}

MANDATE: review EVERY hunk of that diff - no sampling, no skimming. Count the hunks and report hunks_reviewed. For each hunk, Read the changed file around the change for context before judging it.

WHAT TO CHECK (in priority order):
1. Semantic equivalence: a diagnostics fix must not change behavior. Watch for flipped comparisons or precedence, changed loop bounds, changed error paths, moved locks or guards changing drop order, borrow or copy changes altering what data is snapshotted, numeric casts changing wrap or precision, iterator versus index differences.
2. Suppressions: every inline suppression in your chunk - is the stated reason TRUE about the code it sits on, is the scope minimal, is it really unavoidable? Flag any whose reason is wrong or that dodges a real fix. Also flag a comment that restates a suppression's reason.
3. Completeness at callsites: signature, async or type changes must be consistent at ALL callsites - verify by reading callers with symbol-aware tools or Read.
4. Deletions: confirm a deleted item has no remaining references anywhere (tests, benches, other packages).
5. Convention conformance: no new explanatory comments added by the fix; the project's own rules (already in your context) hold.${args.focus ? `\n\nSOURCE-SPECIFIC FOCUS:\n${args.focus}` : ''}${c.extra ? `\n\nCHUNK-SPECIFIC NOTE:\n${c.extra}` : ''}${args.conventions ? `\n\n${args.conventions}` : ''}

NEVER use grep, rg, head or tail; use Read with offset/limit or symbol-aware tools. You may consult git log and git show for the commits involved.
Report ONLY real defects with concrete evidence (quote the hunk or cite what you read). Severity: blocker = behavior changed or API broken; major = wrong or misleading suppression, missed callsite, risky rewrite; nit = cosmetic. If everything checks out, verdict=clean with an empty findings list.
You MUST finish by calling the StructuredOutput tool - a run that ends without it is treated as failed.`
}

const width = args.jobs > 0 ? Math.min(args.jobs, chunks.length) : chunks.length
const queue = chunks.map((c, i) => ({ c, i }))
const results = []

async function worker() {
  while (queue.length) {
    const { c, i } = queue.shift()
    const opts = { label: `review:${c.name}`, phase: 'Review', model: MODEL, schema: FINDINGS }
    let r = await agent(reviewPrompt(c), opts)
    if (!r) {
      log(`retry review:${c.name}`)
      r = await agent(reviewPrompt(c), { ...opts, label: `review:${c.name}:retry` })
    }
    results[i] = r
  }
}

phase('Review')
await parallel(Array.from({ length: width }, () => () => worker()))

const ok = results.filter(Boolean)
const tag = (sev) => ok.flatMap((r) => r.findings.filter((f) => f.severity === sev).map((f) => ({ chunk: r.chunk, ...f })))
return {
  chunks_reviewed: ok.length,
  total_hunks: ok.reduce((n, r) => n + (r.hunks_reviewed || 0), 0),
  blockers: tag('blocker'),
  majors: tag('major'),
  nits: tag('nit'),
  per_chunk: ok.map((r) => ({ chunk: r.chunk, verdict: r.verdict, hunks: r.hunks_reviewed, n: r.findings.length })),
  failed_chunks: chunks.filter((c, i) => !results[i]).map((c) => c.name),
}
