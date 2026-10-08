/**
 * Tier-1 verification for the kb-reader plugin.
 *
 * Runs the real plugin module against the real corpus with a minimal fake
 * Cordis context, so a change to the Python JSON contract, the tool schemas, or
 * the registration wiring fails here instead of inside a chat session.
 *
 * Usage:  node tools/kb_qa_plugin/test/plugin.test.mjs
 */

import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

const plugin = await import(new URL('../index.js', import.meta.url).href)

/** Minimal Cordis context capturing what the plugin registers. */
function fakeContext() {
  const tools = new Map()
  const sections = []
  return {
    tools: { register: definition => tools.set(definition.name, definition) },
    systemPrompt: { section: section => sections.push(section) },
    registered: tools,
    sections,
  }
}

const ctx = fakeContext()
await plugin.apply(ctx, { askTimeoutMs: 180_000 })

assert.deepEqual(
  [...ctx.registered.keys()].sort(),
  ['kb_ask', 'kb_library', 'kb_verify_quote'],
  'plugin must register exactly the three reading-desk tools',
)
assert.equal(ctx.sections.length, 1, 'one system-prompt contract must be installed')
assert.match(ctx.sections[0].text, /kb_ask/, 'the contract must name the retrieval tool')
assert.equal(typeof ctx.sections[0].order, 'number', 'the contract needs an explicit order')
// The two load-bearing promises of the contract, pinned so they cannot drift:
// retrieval comes before answering, and answers must carry their provenance.
assert.match(
  ctx.sections[0].text,
  /先调用 kb_ask.*取证，再作答|先 kb_ask.*再动笔组织答案/,
  'the contract must order retrieval before answering',
)
assert.match(
  ctx.sections[0].text,
  /《书名》·章节/,
  'the contract must require book · chapter provenance',
)
assert.match(
  ctx.sections[0].text,
  /回答标明出处/,
  'the contract must state the provenance rule as its own rule',
)

/** Execute one registered tool the way the runtime does. */
async function call(name, args) {
  const definition = ctx.registered.get(name)
  const value = await definition.execute(args, { signal: new AbortController().signal })
  const content = definition.output.render(args, value)
  return { value, text: content.map(block => block.text).join('\n') }
}

const library = await call('kb_library', { limit: 3 })
assert.ok(library.value.shelf.length === 3, 'kb_library must honor its limit')
assert.ok(library.value.global.workspace_count > 50, 'the shelf must report the real corpus')
assert.match(library.text, /藏书/, 'kb_library must render a human shelf')
// A truncated shelf must never read as "this book does not exist": that
// misreading once discarded an entire evidence surface.
assert.match(library.text, /未列出不等于不存在/, 'a truncated shelf must say so explicitly')

const full = await call('kb_library', { limit: 400 })
assert.equal(
  full.value.shelf.length,
  full.value.global.workspace_count,
  'the default limit must be able to list the whole library',
)

const ask = await call('kb_ask', { query: '柄谷行人 交换样式', limit: 3 })
assert.ok(ask.value.hits.length > 0, 'kb_ask must retrieve evidence for an in-corpus topic')
assert.ok(ask.value.context.includes('《'), 'kb_ask must return citation-labelled context')
assert.match(ask.text, /段证据/, 'kb_ask must render a model-facing summary')
assert.match(
  ask.text,
  /《书名》·章节出处/,
  'kb_ask must tell the model to cite book · chapter provenance in the answer',
)
for (const hit of ask.value.hits) {
  assert.equal(typeof hit.workspace, 'string', 'each hit must carry its workspace')
  assert.equal(typeof hit.title, 'string', 'each hit must carry its chapter title')
  assert.equal(typeof hit.content, 'string', 'each hit must carry its passage text')
}

const scoped = await call('kb_ask', { query: '国民性', workspace: '知识库_鲁迅全集', limit: 2 })
assert.ok(
  scoped.value.hits.every(hit => hit.workspace === '知识库_鲁迅全集'),
  'a workspace-scoped ask must not leak other books',
)

// Naming a book must route to it. Content-only discovery cannot reach a book
// whose Chinese title is a foreign phrase, because that title is not part of
// any chunk body to match against.
const routed = await call('kb_ask', {
  query: '日本现代文学的起源里说的内面是什么意思',
  limit: 3,
})
assert.ok(
  routed.value.diagnostics.routed_by_name.some(name => name.startsWith('日本现代文学的起源')),
  'a query naming a book must route to that book by name',
)
assert.ok(
  routed.value.hits.some(hit => hit.workspace.startsWith('日本现代文学的起源')),
  'the name-routed book must contribute evidence',
)

// Routing follows the author too: a query that names 柄谷行人 reaches his
// books even when no chunk repeats his name beside the concept being asked
// about. This is a feature, not leakage — the author was named.
assert.ok(
  ask.value.diagnostics.routed_by_name.length > 0,
  'naming an author must route to that author\'s books',
)
assert.ok(
  ask.value.diagnostics.routed_by_name.every(name => name.includes('柄谷行人')),
  'author routing must only reach workspaces that carry that author',
)

// A query naming nothing must not route anything.
const nameless = await call('kb_ask', { query: '交换样式是什么意思', limit: 3 })
assert.deepEqual(
  nameless.value.diagnostics.routed_by_name,
  [],
  'a query with no book or author name must not route by name',
)

// A bare two-character surname must not drag in an author's whole shelf when it
// is only one word among others.
const conceptQuery = await call('kb_ask', { query: '鲁迅 国民性 批判', limit: 3 })
assert.deepEqual(
  conceptQuery.value.diagnostics.routed_by_name,
  [],
  'a short author surname inside a concept query must not route',
)

// The packaging noise in workspace directory names must never route anything.
const noisy = await call('kb_ask', { query: 'z-library 的 z-lib 版本', limit: 3 })
assert.deepEqual(
  noisy.value.diagnostics.routed_by_name,
  [],
  'source-site noise must not be treated as a book name',
)

const verified = await call('kb_verify_quote', { quote: '从来如此，便对么？' })
assert.equal(verified.value.verdict, 'verbatim', 'a real quotation must verify as verbatim')
assert.match(verified.text, /核验通过/, 'the verdict must be rendered for the model')

// Case is part of what a reader sees on the page, so a case difference must be
// reported rather than folded away and called verbatim. The corpus prints the
// exchange styles as "A.赠与的互酬" (capitalized), so the lowercase form is the
// normalized case and the capitalized form is the verbatim one.
const recased = await call('kb_verify_quote', {
  quote: '交换样式有四种类型：a.赠与的互酬，b.服从与保护，c.商品交换',
})
assert.equal(
  recased.value.verdict,
  'verbatim_normalized',
  'a case-only difference must not be reported as a verbatim match',
)
assert.ok(recased.value.differences.length > 0, 'the difference must be reported')
assert.equal(recased.value.differences[0].quote_char, 'a', 'the differing character is named')
assert.equal(recased.value.differences[0].corpus_char, 'A', 'the corpus form is named')
assert.match(recased.text, /按下面给出的原文写法引用/, 'the model must be told to copy the corpus form')

// Whitespace and full-width punctuation are presentation, not wording.
const exactCase = await call('kb_verify_quote', {
  quote: '交换样式有四种类型：A.赠与的互酬，B.服从与保护，C.商品交换',
})
assert.equal(exactCase.value.verdict, 'verbatim', 'the corpus casing must verify as verbatim')
assert.match(exactCase.text, /核验通过/, 'the verdict must be rendered for the model')

// The excerpt must show the corpus's own casing, never a folded rendering.
assert.ok(
  exactCase.value.locations[0].excerpt.includes('A.赠与的互酬'),
  'the returned excerpt must preserve the corpus casing',
)

const fabricated = await call('kb_verify_quote', {
  quote: '从来如此，便是对的了，我们必须永远如此。',
})
assert.equal(fabricated.value.verdict, 'mismatch', 'a doctored quotation must not verify')
assert.match(fabricated.text, /核验失败/, 'a failed verdict must be rendered plainly')

// A genuinely absent topic: Latin tokens that occur nowhere in the corpus. A
// composed Chinese phrase would NOT be a valid probe here — FTS5 trigram
// matching legitimately finds its characters in ordinary prose, and the corpus
// then really does contain those words.
const empty = await call('kb_ask', { query: 'zzzqx nonexistenttoken zzzqx' })
assert.equal(empty.value.hits.length, 0, 'an absent topic must return no hits')
assert.match(empty.text, /没有检索到/, 'an empty result must tell the model not to invent')

// The vector stage alone would return nearest neighbours for that query; the
// gate is what stops them from being reported as evidence.
const gated = await call('kb_ask', { query: 'zzzqx nonexistenttoken zzzqx', semantic: true })
assert.equal(gated.value.hits.length, 0, 'semantic retrieval must not bypass the evidence gate')
// An empty result must say what to try next rather than returning silence.
assert.ok(
  Array.isArray(gated.value.diagnostics.hints) && gated.value.diagnostics.hints.length > 0,
  'an empty result must carry actionable hints',
)
console.log('kb-reader plugin: all assertions passed')
console.log(`  shelf sample: ${library.value.shelf.map(e => e.workspace).join(' / ')}`)
console.log(`  ask hits: ${ask.value.hits.map(h => h.book.slice(0, 22)).join(' | ')}`)
console.log(`  routed by name: ${routed.value.diagnostics.routed_by_name.length} book(s)`)
