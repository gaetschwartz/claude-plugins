import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

const TOOL = 'mcp__isolated-agents__isolated_agent' as never
const TYPE = 'isolated-agents:w-test'
const SCRATCH = '/tmp/isolated-agent.TEST0001'
const USAGE = {
  input_tokens: 1,
  output_tokens: 1,
  cache_creation_input_tokens: 0,
  cache_read_input_tokens: 0,
}
const FILES: Record<string, string> = {
  '/work/brief.md': 'Summarise @/work/data.txt in one line.',
  '/work/data.txt': 'alpha beta',
  '/work/extra.txt': 'extra text',
  '/home/u/note.txt': 'home note',
}

type Seen = { spawnCwd?: string; prompt?: string; model?: string; completions: number }

function world(on: On, isAnswered = true): Seen {
  const seen: Seen = { completions: 0 }
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/u' : undefined }))
  on('session.cwd', () => ({ value: '/work' }))
  on('session.model', () => ({ value: 'parent-model' }))
  on('process.run', (_, e) =>
    ({ value: { exitCode: 0, stdout: e.argv[0] === 'mktemp' ? `${SCRATCH}\n` : '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('fs.stat', (_, e) =>
    e.path in FILES
      ? { value: { kind: 'file', size: FILES[e.path]!.length, mtimeMs: 0, isLink: false, realPath: e.path } }
      : { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false, realPath: e.path } })
  on('fs.read', (_, e) => ({ value: FILES[e.path] ?? '' }))
  on('agent.list', () => ({ value: [{ id: 'a1', description: 'w', type: TYPE, status: 'running' }] }))
  on('agent.spawn', (_, e) => {
    seen.spawnCwd = e.cwd
    return { model: 'sonnet', agentId: 'a1' }
  })
  on('model.complete', (_, e) => {
    seen.completions++
    seen.prompt = e.prompt
    seen.model = e.model
    return {
      value: isAnswered
        ? { isAnswered: true, text: 'blind answer', usage: USAGE }
        : { isAnswered: false, reason: 'empty-reply', usage: USAGE },
    } as never
  })
  on('tool.call', () => ({ result: 'ok' }))
  return seen
}

test('complete mode is one bare completion on the session model', async ($, on) => {
  const seen = world(on)
  const ran = await $.tool.call({ tool: TOOL, prompt: 'hi' } as never)

  expect(ran.result).toBe('blind answer')
  expect(seen.model).toBe('parent-model')
})

test('an explicit model wins over the session model', async ($, on) => {
  const seen = world(on)
  await $.tool.call({ tool: TOOL, prompt: 'hi', model: 'haiku' } as never)

  expect(seen.model).toBe('haiku')
})

test('an unanswered completion is refused with its reason', async ($, on) => {
  world(on, false)
  const ran = await $.tool.call({ tool: TOOL, prompt: 'hi' } as never)

  expect(ran.deny).toContain('empty-reply')
})

test('promptFile is the brief and @path tokens inside it are inlined', async ($, on) => {
  const seen = world(on)
  await $.tool.call({ tool: TOOL, promptFile: '/work/brief.md', prompt: 'Be brief.' } as never)

  expect(seen.prompt).toContain('Summarise <file path="/work/data.txt">\nalpha beta\n</file> in one line.')
  expect(seen.prompt).toContain('Be brief.')
})

test('relative and home paths resolve, a missing @token stays literal', async ($, on) => {
  const seen = world(on)
  await $.tool.call({ tool: TOOL, prompt: 'see @./data.txt and @~/note.txt and @/nope/x.txt' } as never)

  expect(seen.prompt).toContain('<file path="/work/data.txt">')
  expect(seen.prompt).toContain('<file path="/home/u/note.txt">')
  expect(seen.prompt).toContain('@/nope/x.txt')
})

test('files are attached once even when the prompt also names them', async ($, on) => {
  const seen = world(on)
  await $.tool.call({ tool: TOOL, prompt: 'use @/work/data.txt', files: ['/work/data.txt', '/work/extra.txt'] } as never)

  expect(seen.prompt?.match(/alpha beta/g)?.length).toBe(1)
  expect(seen.prompt).toContain('Attached files:')
  expect(seen.prompt).toContain('extra text')
})

test('a file that does not exist is refused, not skipped', async ($, on) => {
  const seen = world(on)
  const ran = await $.tool.call({ tool: TOOL, prompt: 'x', files: ['/work/missing.txt'] } as never)

  expect(ran.deny).toContain('missing.txt')
  expect(seen.completions).toBe(0)
})

test('an empty brief is refused', async ($, on) => {
  world(on)
  const ran = await $.tool.call({ tool: TOOL } as never)

  expect(ran.deny).toContain('prompt')
})

test('agent mode refuses empty tools and tools that would escape', async ($, on) => {
  world(on)
  const none = await $.tool.call({ tool: TOOL, mode: 'agent', prompt: 'x', tools: [] } as never)
  const nested = await $.tool.call({ tool: TOOL, mode: 'agent', prompt: 'x', tools: ['Read', 'Agent'] } as never)

  expect(none.deny).toContain('empty')
  expect(nested.deny).toContain('Agent')
})

test('a scratch worker starts in a fresh directory and is confined to it', async ($, on) => {
  const seen = world(on)
  const started = await $.agent.spawn({ subagentType: TYPE, prompt: 'work' } as never)

  expect(started.agentId).toBe('a1')
  expect(seen.spawnCwd).toBe(SCRATCH)

  const inside = await $.tool.call({ tool: 'Read', file_path: `${SCRATCH}/notes.txt`, agentId: 'a1' })
  const outside = await $.tool.call({ tool: 'Read', file_path: '/etc/hostname', agentId: 'a1' })
  const sibling = await $.tool.call({ tool: 'Write', file_path: `${SCRATCH}-other/x`, content: 'x', agentId: 'a1' })
  const notebook = await $.tool.call({ tool: 'NotebookEdit', notebook_path: '/etc/x.ipynb', agentId: 'a1' } as never)
  const mainThread = await $.tool.call({ tool: 'Read', file_path: '/etc/hostname' })

  expect(inside.result).toBe('ok')
  expect(outside.deny).toContain('confined')
  expect(sibling.deny).toContain('confined')
  expect(notebook.deny).toContain('confined')
  expect(mainThread.result).toBe('ok')
})
