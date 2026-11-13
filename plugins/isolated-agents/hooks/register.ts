import type { EngineInterface, Register } from 'claude-code'

const PLUGIN = 'isolated-agents'
const TOOL = 'isolated_agent'
const TOOL_FULL = `mcp__${PLUGIN}__${TOOL}`
const TYPE_PREFIX = `${PLUGIN}:`
const DIR_PREFIX = 'isolated-agent.'
const DEFAULT_TOOLS = ['Read', 'Write', 'Edit']
const FORBIDDEN_TOOLS = new Set(['Agent', 'Task', 'Workflow'])
const TOOL_NAME = /^[A-Za-z0-9_-]+$/
const MAX_FILE_BYTES = 1_048_576
const MAX_TOTAL_BYTES = 4_194_304
const POLL_SECONDS = '1'
const AGENT_TIMEOUT_MS = 600_000
const FILE_TOKEN = /(^|[\s(["'])@((?:~|\.{1,2})?\/[^\s)\]"',;]+)/g

const AGENT_PROMPT = [
  'You are an isolated worker. Your working directory is a scratch directory.',
  'You have no access to any other project, repository, notes or instructions: everything you may rely on is in the task you are given and in the files under inputs/ in your working directory, if any.',
  'File tools (Read, Write, Edit) work only inside the scratch directory; refer to files by relative path.',
  'Finish the task, then reply with the complete result as your final message: the scratch directory is deleted afterwards, so put anything the caller needs in that reply.',
].join('\n')

const DESCRIPTION = [
  "Runs a task in context isolation: the worker sees none of this session's CLAUDE.md files, rules, memory, settings or codebase, only the brief you give it. Put every fact it needs in the brief.",
  'The brief is `prompt`, `promptFile` (a text file read as the brief; write long briefs to a file first, edit them, then pass the path), or both. Inside the brief, `@/abs/path`, `@./rel/path` and `@~/path` tokens naming an existing text file are expanded; `files` attaches more. Text files only, 1 MiB each, 4 MiB total. Relative paths resolve against the session working directory.',
  'mode "complete" (default): one model completion with no tools and no history; files are inlined into the prompt.',
  'mode "agent": a subagent started in a fresh temp directory; files are copied into its inputs/ folder. `tools` picks what it may call (default Read, Write, Edit) and `skills` preloads skills. Read, Write, Edit and NotebookEdit are confined to the temp directory; any other tool you grant (Bash, WebFetch, MCP tools...) is NOT path-confined and may be refused for lack of anyone to approve it. The directory is deleted when it finishes unless `keep` is true.',
  'model defaults to this session\'s model.',
].join('\n')

const INPUT_SCHEMA = {
  type: 'object',
  properties: {
    prompt: { type: 'string', description: 'The task. Optional when promptFile is given; appended after it otherwise.' },
    promptFile: { type: 'string', description: 'Path of a text file whose content is the brief.' },
    files: { type: 'array', items: { type: 'string' }, description: 'Paths of text files to hand to the worker (inlined in complete mode, copied to inputs/ in agent mode).' },
    mode: { type: 'string', enum: ['complete', 'agent'], description: 'Default "complete".' },
    system: { type: 'string', description: 'complete mode only: an optional system prompt.' },
    model: { type: 'string', description: "Model alias or id. Default: this session's model." },
    effort: { type: 'string', enum: ['low', 'medium', 'high', 'xhigh', 'max'], description: 'complete mode only.' },
    maxTokens: { type: 'integer', description: 'complete mode only: reply token cap. Default 8192.' },
    timeoutMs: { type: 'integer', description: 'Give up after this many milliseconds (agent mode default 600000).' },
    tools: { type: 'array', items: { type: 'string' }, description: 'agent mode only: tool names the worker may call, e.g. ["Read","Write","Edit","Bash","mcp__x__y"]. Default Read, Write, Edit. Agent, Task and Workflow are refused.' },
    skills: { type: 'array', items: { type: 'string' }, description: 'agent mode only: skill names preloaded into the worker.' },
    keep: { type: 'boolean', description: 'agent mode only: keep the scratch directory and report its path.' },
  },
} as const

type Input = {
  prompt?: string
  promptFile?: string
  files?: string[]
  mode?: 'complete' | 'agent'
  system?: string
  model?: string
  effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max'
  maxTokens?: number
  timeoutMs?: number
  tools?: string[]
  skills?: string[]
  keep?: boolean
}

type Loaded = { abs: string; text: string }
type Place = (file: Loaded) => Promise<string>

const dirByAgent = new Map<string, string>()
const pendingDir = new Map<string, string>()
const created = new Set<string>()
const finished = new Map<string, { answer: string; reason: string }>()
const registered = new Set<string>()

async function makeScratch($: EngineInterface): Promise<string> {
  const base = ((await $.env.get('TMPDIR')) ?? '/tmp').replace(/\/$/, '')
  const made = await $.process.run(['mktemp', '-d', `${base}/${DIR_PREFIX}XXXXXXXX`])
  const dir = made.stdout.trim()
  if (made.exitCode !== 0 || !dir) throw new Error(`mktemp failed: ${made.stderr.trim()}`)
  const real = (await $.fs.stat(dir, { resolve: true })).realPath
  if (real === undefined) throw new Error('scratch directory did not resolve')
  created.add(real)
  return real
}

async function removeScratch($: EngineInterface, dir: string): Promise<void> {
  if (!created.has(dir) || !dir.split('/').pop()?.startsWith(DIR_PREFIX)) return
  created.delete(dir)
  await $.process.run(['rm', '-rf', '--', dir])
}

async function placed($: EngineInterface, path: string): Promise<string | undefined> {
  const cut = path.lastIndexOf('/')
  const name = path.slice(cut + 1)
  if (name === '' || name === '.' || name === '..') return undefined
  const own = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
  if (own) return own.realPath
  const folder = cut < 0 ? '.' : path.slice(0, cut + 1)
  const dir = await $.fs.stat(folder, { resolve: true }).catch(() => undefined)
  return dir?.realPath === undefined ? undefined : `${dir.realPath.replace(/\/$/, '')}/${name}`
}

async function isOurs($: EngineInterface, agentId: string): Promise<boolean> {
  if (dirByAgent.has(agentId)) return true
  const agents = await $.agent.list()
  return agents.some(a => a.id === agentId && a.type.startsWith(TYPE_PREFIX))
}

function isInside(real: string, dir: string | undefined): boolean {
  if (dir !== undefined) return real.startsWith(`${dir}/`)
  return [...created].some(d => real.startsWith(`${d}/`))
}

async function confine<R>(
  $: EngineInterface,
  e: { tool: string; agentId?: string },
  path: string,
  pass: () => R | Promise<R>,
): Promise<R | { deny: string }> {
  if (e.agentId === undefined || !(await isOurs($, e.agentId))) return pass()
  const real = await placed($, path)
  return real !== undefined && isInside(real, dirByAgent.get(e.agentId))
    ? pass()
    : { deny: `${PLUGIN}: ${e.tool} is confined to the scratch directory; use a relative path.` }
}

async function resolvePath($: EngineInterface, raw: string): Promise<string> {
  if (raw.startsWith('~/')) {
    const home = await $.env.get('HOME')
    if (home) return `${home.replace(/\/$/, '')}/${raw.slice(2)}`
  }
  if (raw.startsWith('/')) return raw
  return `${(await $.session.cwd()).replace(/\/$/, '')}/${raw}`
}

function fileLoader($: EngineInterface) {
  const cache = new Map<string, Loaded | undefined>()
  let left = MAX_TOTAL_BYTES

  return async (raw: string, isStrict: boolean): Promise<Loaded | undefined> => {
    const path = await resolvePath($, raw)
    if (!cache.has(path)) {
      const stat = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
      if (stat === undefined || stat.kind !== 'file') {
        cache.set(path, undefined)
      } else {
        if (stat.size > MAX_FILE_BYTES) throw new Error(`${raw} is larger than ${MAX_FILE_BYTES} bytes`)
        left -= stat.size
        if (left < 0) throw new Error(`the files total more than ${MAX_TOTAL_BYTES} bytes`)
        const text = await $.fs.read(path)
        if (text.includes('\0')) throw new Error(`${raw} is not a text file`)
        cache.set(path, { abs: stat.realPath ?? path, text })
      }
    }
    const file = cache.get(path)
    if (file === undefined && isStrict) throw new Error(`${raw} is not a readable file`)
    return file
  }
}

async function buildBrief(
  $: EngineInterface,
  input: Input,
  place: Place,
  attachedHeading: string,
  describeAttached: (placedAs: string[]) => string,
): Promise<string> {
  const load = fileLoader($)
  let brief = ''
  if (input.promptFile) brief += (await load(input.promptFile, true))?.text ?? ''
  if (input.prompt) brief += `${brief ? '\n\n' : ''}${input.prompt}`
  if (!brief.trim()) throw new Error('give a non-empty prompt or promptFile')

  const used = new Set<string>()
  let out = ''
  let last = 0
  for (const m of brief.matchAll(FILE_TOKEN)) {
    const start = m.index ?? 0
    const lead = m[1] ?? ''
    const raw = m[2] ?? ''
    const file = await load(raw, false)
    out += brief.slice(last, start + lead.length)
    out += file === undefined ? `@${raw}` : await place(file)
    if (file !== undefined) used.add(file.abs)
    last = start + m[0].length
  }
  out += brief.slice(last)

  const attached: string[] = []
  for (const raw of input.files ?? []) {
    const file = await load(raw, true)
    if (file === undefined || used.has(file.abs)) continue
    used.add(file.abs)
    attached.push(await place(file))
  }
  return attached.length === 0 ? out : `${out}\n\n${attachedHeading}\n${describeAttached(attached)}`
}

function validateTools(tools: readonly string[]): void {
  if (tools.length === 0) throw new Error('tools must not be empty; use mode "complete" for a worker with no tools')
  for (const name of tools) {
    if (!TOOL_NAME.test(name)) throw new Error(`"${name}" is not a tool name`)
    if (FORBIDDEN_TOOLS.has(name)) throw new Error(`the worker may not be given ${name}`)
  }
}

async function ensureAgent($: EngineInterface, tools: readonly string[], skills: readonly string[]): Promise<string> {
  const key = JSON.stringify([[...tools].sort(), [...skills].sort()])
  let hash = 5381
  for (const ch of key) hash = ((hash * 33) ^ ch.charCodeAt(0)) >>> 0
  const name = `w-${hash.toString(36)}`
  if (!registered.has(name)) {
    await $.agent.register({
      name,
      description: 'Isolated scratch worker; started by the isolated_agent tool only.',
      prompt: AGENT_PROMPT,
      tools: [...tools],
      ...(skills.length > 0 ? { skills: [...skills] } : {}),
      omitClaudeMd: true,
      permissionMode: 'acceptEdits',
      model: 'inherit',
      maxTurns: 40,
    })
    registered.add(name)
  }
  return `${TYPE_PREFIX}${name}`
}

async function runComplete($: EngineInterface, input: Input) {
  const model = input.model ?? (await $.session.model())
  const prompt = await buildBrief(
    $,
    input,
    async f => `<file path="${f.abs}">\n${f.text}\n</file>`,
    'Attached files:',
    blocks => blocks.join('\n\n'),
  )
  const done = await $.model.complete({
    model,
    prompt,
    system: input.system,
    maxTokens: input.maxTokens ?? 8192,
    effort: input.effort,
    timeoutMs: input.timeoutMs,
  })
  return done.isAnswered
    ? { result: done.text }
    : { deny: `${PLUGIN}: completion failed (${done.reason}).` }
}

async function runAgent($: EngineInterface, input: Input) {
  const tools = input.tools ?? DEFAULT_TOOLS
  const skills = input.skills ?? []
  validateTools(tools)
  const type = await ensureAgent($, tools, skills)

  const dir = await makeScratch($)
  const nonce = Math.random().toString(36).slice(2, 10).padEnd(8, '0')
  let isKept = false
  try {
    const names = new Map<string, string>()
    const taken = new Set<string>()
    const prompt = await buildBrief(
      $,
      input,
      async f => {
        const known = names.get(f.abs)
        if (known !== undefined) return known
        const base = f.abs.split('/').pop() || 'file'
        let rel = `inputs/${base}`
        for (let n = 2; taken.has(rel); n++) rel = `inputs/${n}-${base}`
        taken.add(rel)
        names.set(f.abs, rel)
        await $.fs.write(`${dir}/${rel}`, f.text)
        return rel
      },
      'Input files (relative to your working directory):',
      rels => rels.map(r => `- ${r}`).join('\n'),
    )

    pendingDir.set(nonce, dir)
    const spawned = await $.agent
      .spawn({
        subagentType: type,
        description: `isolated worker #${nonce}`,
        prompt,
        ...(input.model === undefined ? {} : { model: input.model }),
      })
      .finally(() => pendingDir.delete(nonce))
    if ('deny' in spawned && spawned.deny !== undefined) return { deny: `${PLUGIN}: ${spawned.deny}` }
    const agentId = spawned.agentId
    if (agentId === undefined) return { deny: `${PLUGIN}: the worker did not start.` }

    // a `$` call's time is free of the hook's budget, an awaited promise's is not
    const limit = input.timeoutMs ?? AGENT_TIMEOUT_MS
    const startedAt = await $.clock.now()
    while (!finished.has(agentId)) {
      if ((await $.clock.now()) - startedAt > limit) return { deny: `${PLUGIN}: the worker did not finish in ${limit} ms.` }
      await $.process.run(['sleep', POLL_SECONDS])
    }
    const end = finished.get(agentId)
    finished.delete(agentId)
    const answer = end?.answer ?? ''
    if (end?.reason !== 'answer') {
      return { deny: `${PLUGIN}: the worker ended with "${end?.reason}".${answer ? ` Last words: ${answer}` : ''}` }
    }
    if (input.keep === true) {
      isKept = true
      created.delete(dir)
      return { result: `${answer}\n\n[scratch directory kept: ${dir}]` }
    }
    return { result: answer }
  } finally {
    if (!isKept) await removeScratch($, dir)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.tool.register({
      name: TOOL,
      description: DESCRIPTION,
      inputSchema: INPUT_SCHEMA,
      isDeferred: false,
    })
    return next(e)
  })

  on('agent.offer', { agent: /^isolated-agents:/ }, () => ({ isOffered: false }))

  on('turn.complete', ($, e, next) => {
    if (e.agentId !== undefined) finished.set(e.agentId, { answer: e.answer, reason: e.reason })
    return next(e)
  })

  for (const type of ['mcp_instructions_delta', 'skill_listing'] as const) {
    on('prompt.attachment', { type }, async ($, e, next) =>
      e.agentId !== undefined && (await isOurs($, e.agentId)) ? { text: null } : next(e),
    )
  }

  on('session.end', async ($, e, next) => {
    for (const dir of [...created]) await removeScratch($, dir)
    return next(e)
  })

  on('agent.spawn', { subagentType: /^isolated-agents:/ }, async ($, e, next) => {
    const nonce = /#([a-z0-9]{8})$/.exec(e.description)?.[1]
    const cwd = (nonce === undefined ? undefined : pendingDir.get(nonce)) ?? (await makeScratch($))
    const started = await next({ ...e, cwd })
    if ('agentId' in started && started.agentId !== undefined) dirByAgent.set(started.agentId, cwd)
    return started
  })

  on('tool.call', { tool: 'Read' }, ($, e, next) => confine($, e, e.file_path, () => next(e))).catch(($, e, next) =>
    next.called ? next(e) : { deny: `${PLUGIN}: the scratch-directory guard failed.` },
  )
  on('tool.call', { tool: 'Write' }, ($, e, next) => confine($, e, e.file_path, () => next(e))).catch(($, e, next) =>
    next.called ? next(e) : { deny: `${PLUGIN}: the scratch-directory guard failed.` },
  )
  on('tool.call', { tool: 'Edit' }, ($, e, next) => confine($, e, e.file_path, () => next(e))).catch(($, e, next) =>
    next.called ? next(e) : { deny: `${PLUGIN}: the scratch-directory guard failed.` },
  )
  on('tool.call', { tool: 'NotebookEdit' }, ($, e, next) => confine($, e, e.notebook_path, () => next(e))).catch(($, e, next) =>
    next.called ? next(e) : { deny: `${PLUGIN}: the scratch-directory guard failed.` },
  )

  on('tool.call', { tool: TOOL_FULL as never }, async ($, e) => {
    const input = e as unknown as Input
    try {
      return input.mode === 'agent' ? await runAgent($, input) : await runComplete($, input)
    } catch (err) {
      return { deny: `${PLUGIN}: ${err instanceof Error ? err.message : String(err)}` }
    }
  })
}
