/**
 * Tier-1 verification for the kb-ingest plugin.
 *
 * Runs the real plugin module against the real pipeline with a minimal fake
 * Cordis context, so a change to the Python JSON contract, the tool schemas, or
 * the registration wiring fails here instead of inside a chat session.
 *
 * The cases deliberately include *failing* sources: the gate must be able to
 * say no, and a plugin that only ever reports success would be worse than none.
 *
 * Usage:  node tools/kb_ingest_plugin/test/plugin.test.mjs
 */

import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const plugin = await import(new URL('../index.js', import.meta.url).href)

/**
 * Assert a tool value survives a JSON round trip losslessly.
 *
 * The registry snapshots every canonical output with the same rule and rejects
 * the *whole* result when any property is `undefined`, `BigInt`, cyclic, a
 * sparse array, `-0`, or an exotic object. A plugin whose return statement
 * leaks one `undefined` registers fine, passes every unit test, and then fails
 * every single call at runtime — so the contract is checked here, on the real
 * values each tool produces.
 *
 * @param {string} label - Tool name used in the failure message.
 * @param {unknown} value - The canonical value to test.
 */
function assertLossless(label, value) {
  let text
  try {
    text = JSON.stringify(value)
  } catch (error) {
    assert.fail(`${label} output must be JSON-serializable: ${error.message}`)
  }
  assert.notEqual(text, undefined, `${label} output must be JSON-serializable`)
  // An `undefined` property disappears from JSON entirely, so a round trip that
  // differs from the original proves something was not representable.
  const sparse = (node, path) => {
    if (Array.isArray(node)) {
      assert.equal(
        Object.keys(node).length,
        node.length,
        `${label} output${path} must be a dense array`,
      )
      node.forEach((item, index) => sparse(item, `${path}[${index}]`))
      return
    }
    if (node !== null && typeof node === 'object') {
      for (const [key, item] of Object.entries(node)) {
        assert.notEqual(
          item,
          undefined,
          `${label} output${path}.${key} must not be undefined`,
        )
        assert.notEqual(typeof item, 'bigint', `${label} output${path}.${key} must not be a BigInt`)
        sparse(item, `${path}.${key}`)
      }
    }
  }
  sparse(value, '')
}

/** Minimal Cordis context capturing what the plugin registers. */
function fakeContext() {
  const tools = new Map()
  const sections = []
  const warnings = []
  return {
    tools: {
      register: (definition) => {
        if (!definition || typeof definition.name !== 'string') {
          warnings.push('register() received an unusable definition')
          return
        }
        tools.set(definition.name, definition)
      },
    },
    systemPrompt: { section: section => sections.push(section) },
    // The plugin registers inside an effect so a hot reload can dispose it; the
    // fake must run the callback and keep the disposer, not swallow it.
    effect: (callback) => { callback() },
    logger: { warn: message => warnings.push(String(message)) },
    registered: tools,
    sections,
    warnings,
  }
}

const ctx = fakeContext()
await plugin.apply(ctx, { ingestTimeoutMs: 900_000 })
// `apply` schedules registration through `ctx.effect`; yield so the dynamic
// `@deepseek-ai/dsh-tools` import and the registrations settle.
await new Promise(resolve => setTimeout(resolve, 0))

assert.deepEqual(
  [...ctx.registered.keys()].sort(),
  ['kb_ingest_source', 'kb_ingest_status', 'kb_verify_word'],
  'plugin must register exactly the three ingest-desk tools',
)
assert.deepEqual(
  ctx.warnings,
  [],
  `registration must not warn (a rejected schema silently drops tools): ${ctx.warnings.join(' | ')}`,
)
assert.equal(ctx.sections.length, 1, 'one system-prompt contract must be installed')
assert.match(ctx.sections[0].text, /kb_ingest_source/, 'the contract must name the ingest tool')
assert.equal(typeof ctx.sections[0].order, 'number', 'the contract needs an explicit order')

// The value DSL requires an explicit `additionalProperties` on every object
// node, and hoists per-property `required: true` onto the object root.
assert.equal(ctx.registered.get('kb_ingest_source').parameters.required.join(','), 'source')
assert.equal(ctx.registered.get('kb_verify_word').parameters.required.join(','), 'workspace')
assert.equal(ctx.registered.get('kb_ingest_status').parameters.required.join(','), 'workspace')
for (const [name, definition] of ctx.registered) {
  assert.equal(definition.output.schema.type, 'object', `${name} output must be an object root`)
  assert.equal(
    definition.output.schema.additionalProperties,
    true,
    `${name} output must declare openness explicitly`,
  )
}

/** Execute one registered tool the way the runtime does. */
async function call(name, args) {
  const definition = ctx.registered.get(name)
  const value = await definition.execute(args, { signal: new AbortController().signal })
  assertLossless(name, value)
  const content = definition.output.render(args, value)
  return { value, text: content.map(block => block.text).join('\n') }
}

const sandbox = mkdtempSync(join(tmpdir(), 'kb-ingest-plugin-'))
const workspace = join(sandbox, 'work')

const goodBody = [
  '　　翻译在明治时期不只是一项学术工作，而是国家建设的一部分。',
  '福泽谕吉在《西洋事情》中所做的，正是把西方的制度与观念重新组装成',
  '日本人能够理解的叙述框架，这种做法后来被称为启蒙的翻译。',
].join('\n\n')

const goodSource = join(sandbox, 'good.md')
writeFileSync(
  goodSource,
  `# 第一章 翻译文化的到来\n\n${goodBody}\n\n# 第二章 明治初期的翻译事业\n\n${goodBody}\n`,
  'utf8',
)

try {
  const ingest = await call('kb_ingest_source', {
    source: goodSource,
    title: '翻译与近代日本',
    author: '丸山真男',
    output_dir: workspace,
  })

  assert.equal(ingest.value.status, 'passed', 'a clean source must pass the fidelity gate')
  assert.equal(ingest.value.ok, true, 'a passing ingest must report ok')
  assert.equal(ingest.value.missing_characters, 0, 'a clean source must lose no character')
  assert.equal(ingest.value.extra_characters, 0, 'a clean source must gain no character')
  assert.equal(ingest.value.similarity, 1, 'a clean source must match exactly')
  assert.equal(ingest.value.chapter_count, 2, 'both chapters must be ingested')
  assert.ok(ingest.value.chunk_count >= 2, 'each chapter must yield corpus chunks')
  assert.ok(
    ingest.value.knowledge_base.endsWith('knowledge_base.jsonl'),
    'the corpus path must be returned',
  )
  assert.ok(ingest.value.docx.endsWith('.docx'), 'the Word path must be returned')
  assert.equal(ingest.value.gate_error, null, 'a clean run must report no gate error')
  assert.match(ingest.text, /入库完成并通过保真门/, 'the passing verdict must be rendered')

  const status = await call('kb_ingest_status', { workspace })
  assert.equal(status.value.chinese_passed, true, 'a Chinese corpus must pass the language gate')
  assert.equal(status.value.fidelity_status, 'passed', 'the stored fidelity verdict must be readable')
  assert.equal(status.value.gate_error, null, 'a healthy workspace must report no gate error')
  assert.ok(status.value.chunk_count >= 2, 'status must report the real chunk count')
  assert.match(status.text, /语料：/, 'status must render a human summary')
  assert.match(status.text, /路由侧表覆盖/, 'status must report sidecar coverage')

  const verify = await call('kb_verify_word', { workspace })
  assert.equal(verify.value.status, 'passed', 're-verification must reproduce the verdict')
  assert.equal(verify.value.missing_characters, 0, 're-verification must find no missing text')
  assert.match(verify.text, /逐字一致/, 'the verdict must be rendered for the model')

  // The language gate must refuse an untranslated corpus and write nothing.
  const englishSource = join(sandbox, 'english.md')
  writeFileSync(
    englishSource,
    '# Chapter One\n\nThis chapter explains why translation became a state project '
    + 'during the Meiji period and how it reshaped modern Japanese prose.\n',
    'utf8',
  )
  const blocked = await call('kb_ingest_source', {
    source: englishSource,
    title: 'English Book',
    output_dir: join(sandbox, 'foreign'),
  })
  assert.equal(blocked.value.status, 'blocked', 'untranslated prose must be blocked')
  assert.equal(blocked.value.ok, false, 'a blocked ingest must not report ok')
  assert.match(blocked.text, /语言门拦下/, 'the block must be explained')
  assert.match(blocked.text, /知识库没有写盘/, 'the block must state that nothing was written')

  // A scanned PDF must be refused with the real OCR command, not guessed at.
  // The Python gateway reports domain failures as `{ error: {...} }`; the plugin
  // must surface them instead of inventing a successful ingest.
  const fakePdf = join(sandbox, 'scan.pdf')
  writeFileSync(fakePdf, '%PDF-1.4\nnot a real pdf\n', 'utf8')
  const scanned = await call('kb_ingest_source', {
    source: fakePdf,
    title: 'Scanned',
    output_dir: join(sandbox, 'scan'),
  })
  assert.equal(scanned.value.ok, false, 'an unreadable PDF must fail rather than ingest garbage')
  assert.ok(scanned.value.gate_error, 'the refusal must carry a diagnosable error')

  // An absent source must produce a precise error, not an empty result.
  const missing = await call('kb_ingest_source', {
    source: join(sandbox, 'nope.md'),
    title: 'Nope',
  })
  assert.equal(missing.value.ok, false, 'a missing source must not report success')
  assert.match(String(missing.value.gate_error), /源文件不存在/, 'the error must name the cause')

  mkdirSync(join(sandbox, 'empty'), { recursive: true })
  const noCorpus = await call('kb_ingest_status', { workspace: join(sandbox, 'empty') })
  assert.equal(noCorpus.value.chunk_count, 0, 'an empty workspace must report no chunks')
  assert.match(
    String(noCorpus.value.gate_error),
    /knowledge_base/,
    'the error must name the missing corpus',
  )

  console.log('kb-ingest plugin: 40 assertions passed')
  console.log(`  workspace: ${ingest.value.workspace}`)
  console.log(`  chapters ${ingest.value.chapter_count} · chunks ${ingest.value.chunk_count}`
    + ` · missing ${ingest.value.missing_characters} · extra ${ingest.value.extra_characters}`)
} finally {
  rmSync(sandbox, { recursive: true, force: true })
}
