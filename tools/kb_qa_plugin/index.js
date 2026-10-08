/**
 * Philosophy and literature reading desk over this repository's book corpus.
 *
 * The plugin owns three model-facing tools and one standing system-prompt
 * contract. It retrieves nothing itself: every call is answered by `kb_qa.py`,
 * which fronts the two retrieval layers this repository already tests — the
 * repository-wide SQLite/FTS5 index (`global_knowledge_base.py`) and each
 * workspace's BM25 + embedding RAG index (`rag_knowledge_base.py`).
 *
 * Why a subprocess: the corpus, its apparatus weights, its OpenCC
 * normalization and its embedding-provider configuration live in Python.
 * Re-implementing ranking in JavaScript would fork the retrieval contract and
 * silently diverge from the published `evaluate` baselines, so this module
 * stays a thin, cancellable adapter.
 *
 * @module dsh-kb-reader
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

/** Cordis plugin name used by loader diagnostics. */
export const name = 'kb-reader'

/** Services this plugin needs before `apply` runs. */
export const inject = ['tools', 'systemPrompt']

/** Directory holding this plugin module (`tools/kb_qa_plugin/`). */
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))

/** Repository root, derived from the bundle location so a move cannot desync them. */
const REPO_ROOT = resolve(PLUGIN_DIR, '..', '..')

/** Upper bound on passages one `kb_ask` call may return. */
const MAX_HITS = 12

/** Upper bound on the passage budget one `kb_ask` call may request. */
const MAX_CHARS = 80_000

/** Ordinal position of the retrieval contract among system-prompt sections. */
const CONTRACT_ORDER = 118

/** Standing retrieval contract, injected into every session's system prompt. */
const CONTRACT = `## 知识库优先（本地书库）

本工作区挂载了一座本地书库（哲学、文学、思想史、文艺理论，含外文译著）。凡阅读类任务——概念辨析、思想家对照、文本细读、术语溯源、出处考证、"书里怎么说"——必须**先调用 kb_ask 在 RAG 侧知识库取证，再作答**，且回答要标明知识库出处：

正式取证统一使用 hybrid（BM25 + 向量召回 + RRF），仅支持 reader。FTS 只发现候选书，不得作为最终证据，也不得用 lexical / semantic-only 绕过；缺少向量或 provider 时报告 hybrid unavailable，不能称为正常检索。

1. 顺序不可颠倒：先 kb_ask 检索拿到原文段落，再动笔组织答案；书目不确定时先 kb_library。不允许跳过检索直接作答，也不允许先凭记忆写成答案再补检索。
2. 优先级：用户问题 > 语料原文 > 语料之间的对照关系 > 你自己的推论。推论必须显式标为推论，不得伪装成书中观点。
3. 回答标明出处：凡来自书库的论断，随文标注《书名》·章节。直接引用用「」，转述不加引号。凡直接引用，先用 kb_verify_quote 核验：verbatim 可照引；verbatim_normalized 要按工具给出的原文写法改写后再引；mismatch / not_found 就改写为转述，或写明"本库未收录此引文"。
4. 语料没有支撑时直说"本库未找到依据"，再另起一段说明那是语料之外的通行说法。宁可少答，不可编造。
5. 区分声音：作者原论、译者措辞、编者导论与索引等装置内容不是一回事；装置块已被降权，不得当作正文证据。
6. 繁简与译名：可用繁体或另一译名重试（库内检索统一繁简字形，但引文仍按原字形核验）。`

/** Narrow one argument to a non-empty string, or undefined. */
function asText(value) {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

/** Read one config string, falling back to an environment variable. */
function configured(value, envName) {
  return asText(value) ?? asText(process.env[envName])
}

/**
 * Resolve the harness tool API this plugin registers through.
 *
 * A bundle loaded from a linked directory can sit outside the profile's
 * `node_modules` chain, where a bare import would fail. The fallback resolves
 * the harness API module from `$DSH_HOME/profiles/node_modules`, which the
 * launcher keeps in sync with the installation.
 *
 * @returns {Promise<{ defineTool: Function }>} The harness tool API.
 * @throws {Error} When neither location provides a usable `defineTool`.
 */
async function loadToolApi() {
  try {
    const api = await import('@deepseek-ai/dsh-tools')
    if (typeof api.defineTool === 'function') return api
  } catch { /* fall through to the profile fallback below */ }

  const home = process.env.DSH_HOME
  const candidates = [
    home ? join(home, 'profiles', 'node_modules') : undefined,
    process.env.DSH_PROFILE_DIR ? join(process.env.DSH_PROFILE_DIR, 'node_modules') : undefined,
  ].filter(Boolean)
  for (const candidate of candidates) {
    const entry = join(candidate, '@deepseek-ai', 'dsh-tools', 'lib', 'index.js')
    if (!existsSync(entry)) continue
    const api = await import(pathToFileURL(entry).href)
    if (typeof api.defineTool === 'function') return api
  }
  throw new Error(
    'kb-reader: cannot resolve @deepseek-ai/dsh-tools. Install the bundle with '
    + '"dsh plugin --profile <name> add <this-directory>" so it joins the profile module chain.',
  )
}

/**
 * Locate the Python interpreter that owns this repository's dependencies.
 *
 * Order: explicit config, `DSH_KB_PYTHON`, this checkout's virtual environment,
 * then `python` on PATH. The corpus needs the project's own dependencies
 * (OpenCC, the embedding client), so a bare system interpreter is the last
 * resort rather than the default.
 *
 * @param {unknown} configuredPython - `python` from the loader config.
 * @returns {string} An executable name or absolute path.
 */
function resolvePython(configuredPython) {
  const explicit = configured(configuredPython, 'DSH_KB_PYTHON')
  if (explicit) return explicit
  const candidates = [
    join(REPO_ROOT, '.venv', 'Scripts', 'python.exe'),
    join(REPO_ROOT, '.venv', 'bin', 'python'),
    join(REPO_ROOT, 'venv', 'Scripts', 'python.exe'),
  ]
  return candidates.find(existsSync) ?? 'python'
}

/**
 * Resolve the retrieval script, failing loudly when the bundle is incomplete.
 *
 * @param {unknown} configuredScript - `script` from the loader config.
 * @returns {string} Absolute path to `kb_qa.py`.
 */
function resolveScript(configuredScript) {
  const path = configured(configuredScript, 'DSH_KB_SCRIPT') ?? join(PLUGIN_DIR, 'kb_qa.py')
  if (!existsSync(path)) throw new Error(`kb-reader: retrieval script not found at ${path}`)
  return path
}

/**
 * Validate a positive-integer config value.
 *
 * @param {string} label - Field name used in the error message.
 * @param {unknown} value - Candidate value.
 * @returns {number} The accepted integer.
 */
function positiveInteger(label, value) {
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`kb-reader: ${label} must be a positive integer`)
  }
  return value
}

/**
 * Run one retrieval request through the Python helper.
 *
 * The request travels as JSON on stdin, not as argv: on Windows an argv string
 * is re-encoded through the console code page and non-ASCII queries arrive
 * mangled, which would silently degrade every Chinese search. stdout carries
 * exactly one JSON document; stderr is surfaced in the thrown error so a
 * degraded index stays diagnosable from the tool result.
 *
 * @param {object} options - Invocation options.
 * @param {string} options.python - Interpreter to run.
 * @param {string} options.script - Absolute path to `kb_qa.py`.
 * @param {string} options.cwd - Working directory (the repository root).
 * @param {object} options.request - `{ command, args }` request document.
 * @param {number} options.timeoutMs - Hard deadline for the child process.
 * @param {AbortSignal} [options.signal] - Caller cancellation.
 * @returns {Promise<object>} The parsed result document.
 */
function invoke({ python, script, cwd, request, timeoutMs, signal }) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(python, [script, '--stdin-json'], {
      cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    let stdout = ''
    let stderr = ''
    let settled = false

    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      if (error) rejectPromise(error)
      else resolvePromise(value)
    }

    const timer = setTimeout(() => {
      child.kill()
      finish(new Error(`kb-reader: retrieval timed out after ${timeoutMs} ms`))
    }, timeoutMs)

    const onAbort = () => {
      child.kill()
      finish(new Error('kb-reader: retrieval cancelled'))
    }
    if (signal) {
      if (signal.aborted) {
        onAbort()
        return
      }
      signal.addEventListener('abort', onAbort, { once: true })
    }

    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })

    child.on('error', (error) => {
      finish(new Error(
        `kb-reader: cannot start "${python}" (${error.message}). `
        + 'Set the plugin config "python" or DSH_KB_PYTHON to this repository\'s interpreter.',
      ))
    })

    child.on('close', (code) => {
      const trimmed = stdout.trim()
      if (trimmed === '') {
        finish(new Error(
          `kb-reader: retrieval produced no output (exit ${code}). ${stderr.trim().slice(0, 800)}`,
        ))
        return
      }
      let parsed
      try {
        parsed = JSON.parse(trimmed)
      } catch (error) {
        finish(new Error(
          `kb-reader: retrieval returned malformed JSON (${error.message}): ${trimmed.slice(0, 400)}`,
        ))
        return
      }
      if (parsed && typeof parsed === 'object' && parsed.error) {
        const kind = parsed.error.kind ?? 'error'
        const text = parsed.error.message ?? 'unknown failure'
        finish(new Error(`kb-reader: ${kind}: ${text}`))
        return
      }
      finish(undefined, parsed)
    })

    child.stdin.on('error', () => { /* the child may exit before the write drains */ })
    child.stdin.end(JSON.stringify(request))
  })
}

/** Human label for one workspace's release state. */
const STATUS_LABEL = {
  passed: '已验收',
  stale: '验收已过期',
  failed: '验收未通过',
  invalid: '验收报告无效',
  missing: '未验收',
}

/**
 * Describe one retrieval result's provenance in the model-facing text.
 *
 * @param {object} hit - One hit from `kb_ask`.
 * @param {number} index - Zero-based position, used for the citation marker.
 * @returns {string} One header line.
 */
function hitHeader(hit, index) {
  const parts = [`[${index + 1}] 《${hit.book ?? hit.workspace}》`]
  if (hit.title) parts.push(`· ${hit.title}`)
  const notes = []
  if (hit.author) notes.push(hit.author)
  if (hit.language) notes.push(hit.language)
  if (hit.channels) notes.push(hit.channels)
  if (hit.stage === 'discovery') notes.push('摘要级')
  if (notes.length > 0) parts.push(`（${notes.join(' / ')}）`)
  return parts.join(' ')
}

/**
 * Render a `kb_ask` result as the text the model reads.
 *
 * @param {object} value - The canonical tool value.
 * @returns {string} Model-facing markdown.
 */
function renderAsk(value) {
  const hits = value.hits ?? []
  const diagnostics = value.diagnostics ?? {}
  const query = value.query ?? ''
  if (hits.length === 0) {
    const lines = [
      `知识库中没有检索到「${query}」的证据。`,
      '不要凭记忆补写书中观点，回答中说明本库未检出相关证据。可以换用原文术语或另一译名（含繁体）重试；或先用 kb_library 确认库里是否有相关著作。',
    ]
    if (diagnostics.effective_mode === 'not_run') lines.push('没有可供 hybrid 精读的候选书，尚未执行 hybrid；不能据此断定正文没有证据。')
    if (diagnostics.partial_coverage) lines.push(`覆盖不完整：${JSON.stringify(diagnostics.skipped ?? [])}`)
    if (diagnostics.degraded) lines.push(`诊断：${diagnostics.degraded}`)
    return lines.join('\n')
  }
  const books = (diagnostics.deep_books ?? []).join('、')
  const lines = [
    `知识库检索到 ${hits.length} 段证据${books ? `（精读：${books}）` : ''}：`,
    ...hits.map((hit, index) => hitHeader(hit, index)),
    '',
    value.context ?? '',
    '',
    '基于以上证据作答：每条来自书库的论断随文标注《书名》·章节出处；直接引用先用 kb_verify_quote 核验；语料未覆盖之处明说。',
  ]
  if (diagnostics.partial_coverage) lines.push(`覆盖不完整：${JSON.stringify(diagnostics.skipped ?? [])}`)
  if (diagnostics.degraded) lines.push(`注意：${diagnostics.degraded}`)
  return lines.join('\n')
}

/**
 * Render one `kb_verify_quote` verdict.
 *
 * @param {object} value - The canonical tool value.
 * @returns {string} Model-facing markdown.
 */
function renderVerify(value) {
  const locations = value.locations ?? []
  const verdict = value.verdict
  const differences = value.differences ?? []
  const head = verdict === 'verbatim'
    ? '核验通过：这句话逐字存在于语料中（仅空白与全角标点归一）。'
    : verdict === 'verbatim_normalized'
      ? '基本通过：语料中有对应段落，但你的写法与原文在大小写等处不完全一致。请按下面给出的原文写法引用。'
      : verdict === 'mismatch'
        ? '核验失败：检索命中了相关段落，但这句话并不逐字出现在其中——它可能是转述、另一种译法，或凭空生成。'
        : '核验失败：语料中没有检索到这句话。'
  const lines = [head, `引文：${value.quote ?? ''}`]
  for (const difference of differences.slice(0, 2)) {
    lines.push(
      `  差异：你的「${difference.quote_char ?? '(缺)'}」对原文「${difference.corpus_char ?? '(缺)'}」`
      + ` ｜ 你的写法：…${difference.quote_context ?? ''}…`
      + ` ｜ 原文：…${difference.corpus_context ?? ''}…`,
    )
  }
  for (const location of locations.slice(0, 4)) {
    const mark = location.matched ? '✔ 命中' : '✘ 未命中'
    const status = STATUS_LABEL[location.report_status] ?? location.report_status
    lines.push(`- ${mark} 《${location.workspace}》· ${location.title}（${status}）`)
    lines.push(`  ${String(location.excerpt ?? '').replace(/\s+/g, ' ').slice(0, 260)}`)
  }
  if (verdict === 'verbatim') lines.push('可以按《书名》·章节 直接引用。')
  else if (verdict === 'verbatim_normalized') lines.push('改用上面的原文写法后再引用。')
  else if (verdict === 'mismatch') lines.push('请改写为转述，或回查原文页重新摘录；不要按当前措辞直接引用。')
  else lines.push('请改写为转述，或明确标注为"语料未收录"。')
  return lines.join('\n')
}

/**
 * Render the shelf listing.
 *
 * @param {object} value - The canonical tool value.
 * @returns {string} Model-facing markdown.
 */
function renderLibrary(value) {
  const meta = value.global ?? {}
  const shelf = value.shelf ?? []
  const kinds = meta.chunks_by_kind ?? {}
  const total = meta.workspace_count
  const lines = [
    `本地书库：${total ?? '?'} 个工作区，${kinds.knowledge_base ?? 0} 个阅读块`
    + `（原文页 ${kinds.source_page ?? 0}，章节存档 ${kinds.chapter_snapshot ?? 0}）。`,
    `索引构建于 ${meta.built_at ?? '未知'}，完整性 ${meta.integrity ?? '未知'}，中文质量门 ${meta.chinese_gate ?? '未知'}。`,
    '',
    `藏书（按可检索块数排序，本次列出 ${shelf.length} 条）：`,
  ]
  for (const entry of shelf) {
    const status = STATUS_LABEL[entry.report_status] ?? entry.report_status
    lines.push(`- ${entry.workspace} — ${entry.reader_chunks} 块（${status}）`)
  }
  if (typeof total === 'number' && total > shelf.length) {
    // This line is load-bearing. A truncated shelf reads as "the library does
    // not have that book", which is the exact false negative that once cost a
    // whole evidence surface; state the remainder and how to reach it.
    lines.push(
      `- ……另有 ${total - shelf.length} 个工作区未列出。未列出不等于不存在：`
      + `用 kb_library(limit=${total}) 取得完整藏书，或直接搜书名。`,
    )
  }
  lines.push('', '用 kb_ask 在这些著作中检索；"未验收"只表示缺少发布报告，不代表内容不可用。')
  return lines.join('\n')
}

/** Tool parameters, declared once so `apply` stays readable. */
const SCHEMAS = {
  ask: {
    query: { type: 'string', required: true, description: '自然语言问题或关键词组。可写繁体或另一译名。' },
    workspace: { type: 'string', description: '限定到某一部书（工作区名，先用 kb_library 查看）。' },
    scope: {
      type: 'string',
      enum: ['reader'],
      description: '正式 hybrid 取证仅支持已出版 reader 层。',
    },
    limit: { type: 'integer', description: `返回段落数，1–${MAX_HITS}。` },
    per_book_cap: { type: 'integer', description: '每部书在发现阶段的上限，默认 1，0 表示不限制。' },
    max_chars: { type: 'integer', description: `全部段落总字符预算，默认 28000，上限 ${MAX_CHARS}。` },
    verified_only: { type: 'boolean', description: '只返回发布验收通过且未过期的工作区。' },
    mode: { type: 'string', enum: ['hybrid'], description: '正式取证固定 hybrid：BM25 + 向量召回 + RRF。' },
  },
  verify: {
    quote: { type: 'string', required: true, description: '要核验的引文原文，不含外层引号。' },
    workspace: { type: 'string', description: '限定到某一部书。' },
  },
  library: {
    limit: { type: 'integer', description: '返回条数上限。' },
  },
}

/** Canonical output schemas, mirroring what each Python command returns. */
const OUTPUTS = {
  ask: {
    type: 'object',
    additionalProperties: false,
    properties: {
      query: { type: 'string' },
      context: { type: 'string' },
      hits: {
        type: 'array',
        items: {
          type: 'object',
          additionalProperties: true,
          properties: {
            id: { type: 'string' },
            workspace: { type: 'string' },
            book: { type: 'string' },
            title: { type: 'string' },
            content: { type: 'string' },
            stage: { type: 'string' },
            channels: { type: 'string' },
          },
        },
      },
      diagnostics: { type: 'object', additionalProperties: true, properties: {} },
      library: { type: 'string' },
    },
  },
  verify: {
    type: 'object',
    additionalProperties: false,
    properties: {
      quote: { type: 'string' },
      verdict: {
        type: 'string',
        enum: ['verbatim', 'verbatim_normalized', 'mismatch', 'not_found'],
      },
      differences: {
        type: 'array',
        items: {
          type: 'object',
          additionalProperties: true,
          properties: {
            quote_char: { type: 'string' },
            corpus_char: { type: 'string' },
            quote_context: { type: 'string' },
            corpus_context: { type: 'string' },
          },
        },
      },
      locations: {
        type: 'array',
        items: {
          type: 'object',
          additionalProperties: true,
          properties: {
            workspace: { type: 'string' },
            title: { type: 'string' },
            matched: { type: 'boolean' },
            match_kind: { type: 'string' },
            report_status: { type: 'string' },
            excerpt: { type: 'string' },
          },
        },
      },
    },
  },
  library: {
    type: 'object',
    additionalProperties: false,
    properties: {
      global: { type: 'object', additionalProperties: true, properties: {} },
      shelf: {
        type: 'array',
        items: {
          type: 'object',
          additionalProperties: true,
          properties: {
            workspace: { type: 'string' },
            reader_chunks: { type: 'integer' },
            report_status: { type: 'string' },
          },
        },
      },
      summary: { type: 'string' },
    },
  },
}

/**
 * Register the reading desk's tools and its system-prompt contract.
 *
 * Registration is awaited by the loader through the returned promise chain, so
 * a failure to resolve the harness tool API surfaces as a load error rather
 * than a silently missing tool.
 *
 * @param {object} ctx - Cordis context carrying `tools` and `systemPrompt`.
 * @param {object} [config] - Loader config for this row.
 * @returns {Promise<void>} Resolves once every tool is registered.
 */
export async function apply(ctx, config = {}) {
  const { defineTool } = await loadToolApi()
  const python = resolvePython(config.python)
  const script = resolveScript(config.script)
  const cwd = configured(config.repoRoot, 'DSH_KB_REPO_ROOT') ?? REPO_ROOT
  const askTimeoutMs = positiveInteger('askTimeoutMs', config.askTimeoutMs ?? 180_000)
  const quoteTimeoutMs = positiveInteger('quoteTimeoutMs', config.quoteTimeoutMs ?? 60_000)
  const libraryTimeoutMs = positiveInteger('libraryTimeoutMs', config.libraryTimeoutMs ?? 30_000)
  // The shelf defaults to the whole library. Listing a prefix of it is what
  // produced the worst observed failure — a book absent from a truncated list
  // read as a book the library does not have, and a whole evidence surface was
  // never searched for. A few hundred lines of titles cost far less than that.
  const shelfLimit = positiveInteger('shelfLimit', config.shelfLimit ?? 400)
  const defaultHits = positiveInteger('defaultHits', config.defaultHits ?? 6)
  const defaultMaxChars = positiveInteger('defaultMaxChars', config.defaultMaxChars ?? 28_000)

  if (config.contract !== false) {
    ctx.systemPrompt.section({ name: 'kb-reader:contract', order: CONTRACT_ORDER, text: CONTRACT })
  }

  ctx.tools.register(defineTool({
    name: 'kb_ask',
    description:
      '在本地书库（哲学、文学、思想史、文艺理论共 60 余部著作）中检索证据，返回带书名与章节定位的原文段落。'
      + '阅读类问题必须先调用它取证、再基于返回段落作答，回答中随文标注《书名》·章节出处。',
    parameters: SCHEMAS.ask,
    output: {
      schema: OUTPUTS.ask,
      render: (_args, value) => [{ type: 'text', text: renderAsk(value) }],
    },
    timeoutMs: askTimeoutMs,
    // Read-only against an immutable on-disk corpus.
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      if (args.semantic === false || args.deep === false ||
          (args.mode !== undefined && args.mode !== 'hybrid') ||
          (args.scope !== undefined && args.scope !== 'reader')) {
        throw new Error('kb_ask requires hybrid mode, deep retrieval and scope=reader')
      }
      const limit = Math.min(Math.max(Math.trunc(args.limit ?? defaultHits), 1), MAX_HITS)
      const maxChars = Math.min(
        Math.max(Math.trunc(args.max_chars ?? defaultMaxChars), 1_000),
        MAX_CHARS,
      )
      const result = await invoke({
        python,
        script,
        cwd,
        timeoutMs: askTimeoutMs,
        signal: exec.signal,
        request: {
          command: 'ask',
          args: {
            query: args.query,
            workspace: args.workspace ?? null,
            scope: args.scope ?? 'reader',
            limit,
            per_book_cap: Math.max(Math.trunc(args.per_book_cap ?? 1), 0),
            max_chars: maxChars,
            verified_only: args.verified_only === true,
            semantic: true,
            deep: true,
          },
        },
      })
      const shelf = Array.isArray(result.shelf) ? result.shelf : []
      return {
        query: result.query ?? args.query,
        context: result.context ?? '',
        hits: Array.isArray(result.hits) ? result.hits : [],
        diagnostics: result.diagnostics ?? {},
        library: shelf.length > 0
          ? shelf.map(entry => `${entry.workspace}(${entry.reader_chunks})`).join('、')
          : undefined,
      }
    },
    presentCall: args => ({
      card: 'generic',
      kind: 'search',
      title: args.workspace ? `${args.query} @ ${args.workspace}` : args.query,
      rawInput: args.query,
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'kb_verify_quote',
    description:
      '核验一句直接引用是否逐字存在于本地书库中。返回 verbatim（可逐字引用）、'
      + 'verbatim_normalized（语料有对应段落，但大小写等处写法不同，需按原文改写）、'
      + 'mismatch（命中了相关段落但措辞不符）或 not_found（语料中不存在）。直接引用前必须调用。',
    parameters: SCHEMAS.verify,
    output: {
      schema: OUTPUTS.verify,
      render: (_args, value) => [{ type: 'text', text: renderVerify(value) }],
    },
    timeoutMs: quoteTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const result = await invoke({
        python,
        script,
        cwd,
        timeoutMs: quoteTimeoutMs,
        signal: exec.signal,
        request: {
          command: 'verify-quote',
          args: { quote: args.quote, workspace: args.workspace ?? null },
        },
      })
      return {
        quote: result.quote ?? args.quote,
        verdict: result.verdict ?? 'not_found',
        differences: Array.isArray(result.differences) ? result.differences : [],
        locations: Array.isArray(result.locations) ? result.locations : [],
      }
    },
    presentCall: args => ({
      card: 'generic',
      kind: 'search',
      title: `核验引文：${String(args.quote ?? '').slice(0, 40)}`,
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'kb_library',
    description:
      '列出本地书库的工作区、规模与发布验收状态。问题涉及的书目不确定、'
      + '或需要按作者或语言定位可检索著作时先调用它。',
    parameters: SCHEMAS.library,
    output: {
      schema: OUTPUTS.library,
      render: (_args, value) => [{ type: 'text', text: value.summary ?? '' }],
    },
    timeoutMs: libraryTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const result = await invoke({
        python,
        script,
        cwd,
        timeoutMs: libraryTimeoutMs,
        signal: exec.signal,
        request: {
          command: 'status',
          args: { limit: Math.max(Math.trunc(args.limit ?? shelfLimit), 1) },
        },
      })
      const shelf = Array.isArray(result.shelf) ? result.shelf : []
      const global = result.global ?? {}
      // Reuse the human renderer so the model and any UI agree on the text.
      return { global, shelf, summary: renderLibrary({ global, shelf }) }
    },
    presentCall: () => ({ card: 'generic', kind: 'search', title: '浏览本地书库' }),
  }))
}

export default { name, inject, apply, CONTRACT }
