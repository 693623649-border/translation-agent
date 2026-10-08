/**
 * Source-to-knowledge-base ingest desk: turn one source file into a Chinese
 * knowledge base and a Word document that provably matches it.
 *
 * The plugin owns three model-facing tools and one standing system-prompt
 * contract. It decides nothing itself: every call is answered by `kb_ingest.py`,
 * which drives this repository's existing publication machinery —
 * `book_pipeline.build_docx` for the Word file, `rag_knowledge_base` for the
 * corpus manifest, `kb_translation.classify_row` for the Chinese language gate,
 * and `publication_verifier`'s normalization chain for the fidelity comparison.
 *
 * Why a subprocess: the layout engine, the language classifier, the apparatus
 * annotations and the canonical text normalization all live in Python. Porting
 * them into JavaScript would fork the publication contract and let the Word
 * output drift from the one the release gate verifies, so the plugin stays a
 * thin, typed, cancellable adapter.
 *
 * @module dsh-kb-ingest
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Cordis plugin name used by loader diagnostics. */
export const name = 'kb-ingest'

/** Services this plugin needs before `apply` runs. */
export const inject = ['tools', 'systemPrompt']

/** Directory holding this plugin module (`tools/kb_ingest_plugin/`). */
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))

/** Repository root, derived from the bundle location so a move cannot desync them. */
const REPO_ROOT = resolve(PLUGIN_DIR, '..', '..')

/** Upper bound on the paragraph chunk budget one ingest may request. */
const MAX_CHUNK_CHARS = 20_000

/** Standing ingest contract, injected into every session's system prompt. */
const CONTRACT = `## 源文件入库（知识库 + 中文 Word）

把一个源文件变成"可检索的知识库 + 可交付的 Word"时，**走 kb_ingest_source，不要手写脚本**：

1. **入库前先定题名与作者。** 书名决定每行的 \`[书名]\` 标题前缀、\`book_id\` 与 Word 封面，事后改名要整库重建。用户没给就先用 kb_ingest_status 看工作区，或直接问。
2. **扫描版 PDF 会被拒绝。** 文字层为空时 kb_ingest_source 直接失败并给出主流水线命令——那是 OCR + 翻译的活，不要用本工具硬凑。EPUB/DOCX/纯文本/Markdown 与带文字层的 PDF 才走这里。
3. **外文内容默认被语言门拦下。** 语料里每个非中文块都会阻断入库（与 translation-agent-kb register 同语义）；先翻译，或让用户明确同意后才用 allow_foreign。
4. **Word 必须逐字等于源文件。** 门返回 \`missing_characters\` / \`extra_characters\` / \`similarity\` 与逐处 diff（hunks）。**不要**为了让门变绿去改 Word、放宽阈值或改源文件来迁就产物：要么修源文件里真正的问题，要么把失败如实报告给用户。
5. **区分三种非零结果：** \`blocked\`（语言门，语料未写盘）、\`failed\`（保真门，产物在盘上但不合格，可用 kb_verify_word 复核）、\`passed\`（可交付）。
6. **装置降权与侧表已自动完成**（apparatus 旁车、meta 路由侧表、RAG 清单）。不要手工编辑 \`knowledge_base.jsonl\`；重跑 ingest 即可。
7. 交付后要跨书检索，再跑 \`python global_knowledge_base.py sync\`；单书检索用 \`translation-agent-kb retrieve\`。`

/**
 * Resolve one config value with an environment fallback.
 *
 * @param {unknown} value - Value from the loader config, when present.
 * @param {string} envName - Environment variable consulted when unset.
 * @returns {string | undefined} The resolved value, or undefined.
 */
function configured(value, envName) {
  if (typeof value === 'string' && value.trim() !== '') return value.trim()
  const fromEnv = process.env[envName]
  if (typeof fromEnv === 'string' && fromEnv.trim() !== '') return fromEnv.trim()
  return undefined
}

/**
 * Locate the Python interpreter that owns this repository's dependencies.
 *
 * Order: explicit config, `DSH_KB_PYTHON`, this checkout's virtual environment,
 * then `python` on PATH. The publisher needs `python-docx`, `Markdown` and the
 * project's own modules, so a bare system interpreter is the last resort.
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
  const found = candidates.find(candidate => existsSync(candidate))
  return found ?? 'python'
}

/**
 * Resolve the ingest script, failing loudly when the bundle is incomplete.
 *
 * @param {unknown} configuredScript - `script` from the loader config.
 * @returns {string} Absolute path to `kb_ingest.py`.
 */
function resolveScript(configuredScript) {
  const path = configured(configuredScript, 'DSH_KB_INGEST_SCRIPT')
    ?? join(PLUGIN_DIR, 'kb_ingest.py')
  if (!existsSync(path)) {
    throw new Error(`kb-ingest: ingest script not found at ${path}`)
  }
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
    throw new Error(`kb-ingest: ${label} must be a positive integer`)
  }
  return value
}

/**
 * Run one ingest request through the Python helper.
 *
 * The request travels as JSON on stdin, not as argv: on Windows an argv string
 * is re-encoded through the console code page and a Chinese book title arrives
 * mangled, which would corrupt both the corpus title prefix and the Word cover.
 * stdout carries exactly one JSON document; anything on stderr is surfaced in
 * the thrown error so a degraded run stays diagnosable.
 *
 * @param {object} options - Invocation options.
 * @param {string} options.python - Interpreter to run.
 * @param {string} options.script - Absolute path to `kb_ingest.py`.
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
      env: {
        ...process.env,
        // Python picks its stdio text encoding from the console code page on
        // Windows, so a UTF-8 JSON request carrying a Chinese path or title is
        // decoded as GBK and arrives as surrogate escapes, which then fail to
        // encode on the way out. Forcing UTF-8 on both ends makes the bridge
        // code-page independent.
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
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
      finish(new Error(`kb-ingest: run timed out after ${timeoutMs} ms`))
    }, timeoutMs)

    const onAbort = () => {
      child.kill()
      finish(new Error('kb-ingest: run cancelled'))
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
        `kb-ingest: cannot start ${python} (${error.message}). `
        + 'Set the plugin config "python" or DSH_KB_PYTHON to this repository\'s interpreter.',
      ))
    })

    child.on('close', (code) => {
      const trimmed = stdout.trim()
      if (trimmed === '') {
        finish(new Error(
          `kb-ingest: produced no output (exit ${code}). ${stderr.trim().slice(0, 800)}`,
        ))
        return
      }
      let parsed
      try {
        parsed = JSON.parse(trimmed)
      } catch (error) {
        finish(new Error(
          `kb-ingest: returned malformed JSON (${error.message}): ${trimmed.slice(0, 400)}`,
        ))
        return
      }
      if (parsed && typeof parsed === 'object' && parsed.error) {
        // A domain verdict (language gate, fidelity gate) also carries the full
        // report, so surface both instead of discarding the diagnostics.
        const { kind, message: text } = parsed.error
        parsed.__gateError = `${kind ?? 'error'}: ${text ?? 'unknown failure'}`
      }
      finish(undefined, parsed)
    })

    child.stdin.on('error', () => { /* the child may exit before the write drains */ })
    child.stdin.end(JSON.stringify(request))
  })
}

/**
 * Summarize an issue list into one line for the model.
 *
 * @param {Array<object>} issues - Issues from a fidelity report.
 * @returns {string} A compact, actionable summary.
 */
function issueSummary(issues) {
  if (!Array.isArray(issues) || issues.length === 0) return '无'
  return issues
    .map(issue => `${issue.code}：${String(issue.message ?? '').trim()}`)
    .join('\n  ')
}

/**
 * Render an `kb_ingest_source` result as the text the model reads.
 *
 * @param {object} value - The canonical tool value.
 * @returns {string} Model-facing markdown.
 */
function renderIngest(value) {
  const {
    status, source, workspace, book_title: bookTitle,
    chapter_count: chapters, chunk_count: chunks,
    missing_characters: missing, extra_characters: extra, similarity,
    language_gate: gate = {}, docx_fidelity: fidelity,
  } = value

  if (status === 'blocked') {
    const languages = (gate.languages ?? []).join('、') || '未知'
    return [
      `入库被语言门拦下：语料含 ${gate.pending_count} 个未翻译的外文块（语言：${languages}）。`,
      '**知识库没有写盘**——这是设计如此，未译外文不得进入数据库。',
      '',
      '可以：先用 translation-agent-kb translate-kb 翻译并重跑；',
      '或确认用户接受原样入库后，用 allow_foreign 重跑。',
    ].join('\n')
  }

  const head = status === 'passed'
    ? `入库完成并通过保真门：《${bookTitle}》`
    : `入库完成但**保真门未通过**：《${bookTitle}》`

  const lines = [
    head,
    `源文件：${source}`,
    `工作区：${workspace}`,
    `章节 ${chapters} 个 · 文本块 ${chunks} 个 · 中文门 ${gate.passed ? '通过' : '未通过'}`,
  ]

  if (fidelity) {
    lines.push(
      `逐字校对：缺字 ${missing} · 多字 ${extra} · 相似度 ${similarity}`,
      `错误：${issueSummary(fidelity.issues)}`,
    )
    const warnings = fidelity.warnings ?? []
    if (warnings.length > 0) {
      lines.push(`警告：${issueSummary(warnings)}`)
    }
    const hunks = fidelity.hunks ?? []
    if (hunks.length > 0) {
      lines.push('', `前 ${Math.min(hunks.length, 5)} 处差异：`)
      for (const hunk of hunks.slice(0, 5)) {
        const gone = hunk.missing_text ? `缺『${hunk.missing_text}』` : ''
        const added = hunk.extra_text ? `多『${hunk.extra_text}』` : ''
        lines.push(`- [${hunk.kind}] ${gone}${gone && added ? ' / ' : ''}${added}　上下文：${hunk.context ?? ''}`)
      }
    }
    if (status !== 'passed') {
      lines.push(
        '',
        '**不要**为了通过门去改 Word、改阈值或回头改源文件迁就产物。'
        + '请修源文件里真正的缺漏，或把上面的 diff 如实报告给用户。',
      )
    }
  } else {
    lines.push('本次未产出 Word（no_word），只入库。')
  }

  return lines.join('\n')
}

/**
 * Render an `kb_verify_word` result as the text the model reads.
 *
 * @param {object} value - The canonical tool value.
 * @returns {string} Model-facing markdown.
 */
function renderVerifyWord(value) {
  const {
    status, docx, missing_characters: missing, extra_characters: extra,
    similarity, issues = [], warnings = [], hunks = [],
  } = value
  const lines = [
    status === 'passed'
      ? '保真门通过：Word 正文与源文件逐字一致。'
      : `保真门未通过：缺字 ${missing}、多字 ${extra}（相似度 ${similarity}）。`,
    `文档：${docx}`,
    `错误：${issueSummary(issues)}`,
  ]
  if (warnings.length > 0) lines.push(`警告：${issueSummary(warnings)}`)
  for (const hunk of hunks.slice(0, 5)) {
    const gone = hunk.missing_text ? `缺『${hunk.missing_text}』` : ''
    const added = hunk.extra_text ? `多『${hunk.extra_text}』` : ''
    lines.push(`- [${hunk.kind}] ${gone}${gone && added ? ' / ' : ''}${added}　上下文：${hunk.context ?? ''}`)
  }
  if (status !== 'passed') {
    lines.push('请修源文件或重新发布，不要直接编辑 DOCX 掩盖差异。')
  }
  return lines.join('\n')
}

/**
 * Render an `kb_ingest_status` result as the text the model reads.
 *
 * @param {object} value - The canonical tool value.
 * @returns {string} Model-facing markdown.
 */
function renderStatus(value) {
  const { workspace, corpus = {}, sidecar = {}, gates = {}, docx = {} } = value
  const chinese = gates.chinese ?? {}
  const fidelity = gates.word_fidelity
  const lines = [
    `工作区：${workspace}`,
    `语料：${corpus.chunk_count} 块 · ${corpus.chapter_count} 章 · ${corpus.characters} 字`,
    `路由侧表覆盖：${((sidecar.coverage ?? 0) * 100).toFixed(1)}%（${sidecar.row_count} 行）`,
    `中文门：${chinese.passed ? '通过' : `未通过，${chinese.pending_count} 块待译`}`,
    `DOCX：${docx.count} 个${docx.count === 1 ? `（${docx.candidates[0]}）` : ''}`,
  ]
  lines.push(fidelity
    ? `Word 保真门：${fidelity.status}（缺字 ${fidelity.missing_characters} · 多字 ${fidelity.extra_characters}）`
    : 'Word 保真门：尚未运行（用 kb_verify_word 复核）')
  lines.push('', '检索：translation-agent-kb retrieve <工作区> "<问题>"；跨书请先 python global_knowledge_base.py sync。')
  return lines.join('\n')
}

/**
 * Register the ingest desk's tools and its system-prompt contract.
 *
 * @param {object} ctx - Cordis context carrying `tools` and `systemPrompt`.
 * @param {object} [config] - Loader config for this row.
 */
export function apply(ctx, config = {}) {
  const python = resolvePython(config.python)
  const script = resolveScript(config.script)
  const cwd = configured(config.repoRoot, 'DSH_KB_REPO_ROOT') ?? REPO_ROOT
  const ingestTimeoutMs = positiveInteger('ingestTimeoutMs', config.ingestTimeoutMs ?? 900_000)
  const verifyTimeoutMs = positiveInteger('verifyTimeoutMs', config.verifyTimeoutMs ?? 300_000)
  const statusTimeoutMs = positiveInteger('statusTimeoutMs', config.statusTimeoutMs ?? 60_000)
  const defaultChunkChars = positiveInteger('defaultChunkChars', config.defaultChunkChars ?? 4000)

  if (config.contract !== false) {
    ctx.systemPrompt.section({ name: 'kb-ingest:contract', order: 119, text: CONTRACT })
  }

  // Imported lazily so a profile that loads this bundle without the harness
  // tool package fails with a precise message instead of a bare resolution error.
  const register = async () => {
    const { defineTool } = await import('@deepseek-ai/dsh-tools')

    ctx.tools.register(defineTool({
      name: 'kb_ingest_source',
      description:
        '把一个源文件（Markdown / 纯文本 / DOCX / EPUB / 带文字层的 PDF）加入本地知识库，'
        + '并产出逐字校对通过的中文 Word。返回缺字、多字、相似度与逐处 diff。'
        + '扫描版 PDF 会被拒绝并给出 OCR 主流水线命令。',
      parameters: {
        source: {
          type: 'string',
          required: true,
          description: '源文件绝对路径或相对工作目录的路径。',
        },
        title: {
          type: 'string',
          description: '书名。决定 [书名] 标题前缀、book_id 与 Word 封面；不填则取源文件名。',
        },
        author: { type: 'string', description: '作者，写入路由侧表并印在封面标题下。' },
        output_dir: {
          type: 'string',
          description: '工作区目录，默认 outputs/<源文件名>。同名已有工作区会被覆盖重建。',
        },
        language: { type: 'string', description: '侧表记录的语言标签，默认 zh。' },
        chunk_chars: {
          type: 'integer',
          description: `单块字符上限（段落边界切分），默认 ${defaultChunkChars}，与主流水线一致。`,
        },
        no_word: { type: 'boolean', description: '只入库，不产出 Word。默认 false。' },
        allow_foreign: {
          type: 'boolean',
          description: '按原样注册未译外文块。默认 false——语言门会拦下并拒绝写盘。',
        },
        allow_line_breaks: {
          type: 'boolean',
          description: '把段内换行降级为警告。仅当源文件本身是诗歌或列表时才用。',
        },
      },
      output: {
        // The value DSL requires `type: 'object'` to declare openness
        // explicitly; omitting `additionalProperties` makes `defineTool` throw
        // and silently drops every tool in this plugin.
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            status: { type: 'string', required: true, enum: ['passed', 'failed', 'blocked'] },
            ok: { type: 'boolean', required: true },
            summary: { type: 'string', required: true },
            workspace: { type: 'string' },
            book_title: { type: 'string' },
            chapter_count: { type: 'integer' },
            chunk_count: { type: 'integer' },
            missing_characters: { type: 'integer' },
            extra_characters: { type: 'integer' },
            similarity: { type: 'number' },
            knowledge_base: { type: 'string' },
            docx: { type: 'string' },
            gate_error: { type: 'string' },
            issues: {
              type: 'array',
              items: {
                type: 'object',
                additionalProperties: true,
                properties: {
                  code: { type: 'string', required: true },
                  message: { type: 'string', required: true },
                },
              },
            },
            hunks: {
              type: 'array',
              items: {
                type: 'object',
                additionalProperties: true,
                properties: {
                  kind: { type: 'string', required: true },
                  missing_text: { type: 'string' },
                  extra_text: { type: 'string' },
                  context: { type: 'string' },
                },
              },
            },
          },
        },
        render: (_args, value) => [{ type: 'text', text: renderIngest(value) }],
        presentationMeta: args => ({ query: args.title ?? args.source }),
      },
      timeoutMs: ingestTimeoutMs,
      // Writes into one workspace; concurrent ingests of the same target would
      // race on knowledge_base.jsonl.
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const chunkChars = Math.min(
          Math.max(Math.trunc(args.chunk_chars ?? defaultChunkChars), 200),
          MAX_CHUNK_CHARS,
        )
        const result = await invoke({
          python,
          script,
          cwd,
          timeoutMs: ingestTimeoutMs,
          signal: exec.signal,
          request: {
            command: 'ingest',
            args: {
              source: args.source,
              title: args.title ?? null,
              author: args.author ?? '',
              output_dir: args.output_dir ?? null,
              language: args.language ?? 'zh',
              chunk_chars: chunkChars,
              no_word: args.no_word === true,
              allow_foreign: args.allow_foreign === true,
              allow_line_breaks: args.allow_line_breaks === true,
            },
          },
        })
        const fidelity = result.docx_fidelity ?? {}
        const artifacts = result.artifacts ?? {}
        // Tool values must be lossless JSON: an `undefined` property is not
        // representable, so every optional string falls back to null (the
        // declared schema) instead of being dropped.
        return {
          status: result.status ?? 'failed',
          ok: result.ok === true,
          summary: renderIngest({ ...result, ...fidelity }),
          workspace: result.workspace ?? null,
          book_title: result.book_title ?? null,
          chapter_count: result.chapter_count ?? null,
          chunk_count: result.chunk_count ?? null,
          missing_characters: fidelity.missing_characters ?? null,
          extra_characters: fidelity.extra_characters ?? null,
          similarity: fidelity.similarity ?? null,
          knowledge_base: artifacts.knowledge_base ?? null,
          docx: artifacts.docx ?? null,
          issues: fidelity.issues ?? [],
          hunks: fidelity.hunks ?? [],
          gate_error: result.__gateError ?? null,
        }
      },
      presentCall: args => ({
        card: 'generic',
        kind: 'search',
        title: `入库：${args.title ?? args.source}`,
        rawInput: args.source,
      }),
      presentResult: (args, result) => (result.isError
        ? undefined
        : { card: 'generic', title: `入库：${args.title ?? args.source}`, content: result.content?.[0]?.text }),
    }))

    ctx.tools.register(defineTool({
      name: 'kb_verify_word',
      description:
        '对已有工作区重跑逐字保真门：证明 Word 正文与源文件一致，返回缺字、多字、'
        + '相似度、逐处 diff 以及段内硬换行统计。交付或怀疑少字时调用。',
      parameters: {
        workspace: {
          type: 'string',
          required: true,
          description: '工作区名称（如 outputs 下的书名）或目录路径。',
        },
        docx: { type: 'string', description: '显式 DOCX 路径；工作区有多个 DOCX 时必须指定。' },
        allow_line_breaks: {
          type: 'boolean',
          description: '把段内换行降级为警告；仅当源文件本身是诗歌或列表时使用。',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            status: { type: 'string', required: true, enum: ['passed', 'failed'] },
            ok: { type: 'boolean', required: true },
            summary: { type: 'string', required: true },
            docx: { type: 'string', required: true },
            missing_characters: { type: 'integer', required: true },
            extra_characters: { type: 'integer', required: true },
            similarity: { type: 'number', required: true },
            issues: {
              type: 'array',
              items: {
                type: 'object',
                additionalProperties: true,
                properties: {
                  code: { type: 'string', required: true },
                  message: { type: 'string', required: true },
                },
              },
            },
          },
        },
        render: (_args, value) => [{ type: 'text', text: renderVerifyWord(value) }],
      },
      timeoutMs: verifyTimeoutMs,
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        const result = await invoke({
          python,
          script,
          cwd,
          timeoutMs: verifyTimeoutMs,
          signal: exec.signal,
          request: {
            command: 'verify-word',
            args: {
              workspace: args.workspace,
              docx: args.docx ?? null,
              allow_line_breaks: args.allow_line_breaks === true,
            },
          },
        })
        return {
          status: result.status ?? 'failed',
          ok: result.ok === true,
          summary: renderVerifyWord(result),
          docx: result.docx ?? '',
          missing_characters: result.missing_characters ?? 0,
          extra_characters: result.extra_characters ?? 0,
          similarity: result.similarity ?? 0,
          issues: result.issues ?? [],
          warnings: result.warnings ?? [],
          hunks: result.hunks ?? [],
        }
      },
      presentCall: args => ({
        card: 'generic',
        kind: 'search',
        title: `校对 Word：${args.workspace}`,
      }),
    }))

    ctx.tools.register(defineTool({
      name: 'kb_ingest_status',
      description:
        '查看一个工作区的语料规模、路由侧表覆盖率、中文门与 Word 保真门的当前状态。'
        + '入库前确认工作区、或交付前确认两道门时调用。',
      parameters: {
        workspace: {
          type: 'string',
          required: true,
          description: '工作区名称（如 outputs 下的书名）或目录路径。',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            summary: { type: 'string', required: true },
            workspace: { type: 'string', required: true },
            chunk_count: { type: 'integer', required: true },
            chinese_passed: { type: 'boolean', required: true },
            fidelity_status: { type: 'string' },
            gate_error: { type: 'string' },
          },
        },
        render: (_args, value) => [{ type: 'text', text: value.summary }],
      },
      timeoutMs: statusTimeoutMs,
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        const result = await invoke({
          python,
          script,
          cwd,
          timeoutMs: statusTimeoutMs,
          signal: exec.signal,
          request: { command: 'status', args: { workspace: args.workspace } },
        })
        const gates = result.gates ?? {}
        return {
          summary: renderStatus(result),
          workspace: result.workspace ?? args.workspace,
          chunk_count: result.corpus?.chunk_count ?? 0,
          chinese_passed: gates.chinese?.passed === true,
          fidelity_status: gates.word_fidelity?.status ?? null,
          gate_error: result.__gateError ?? null,
        }
      },
      presentCall: args => ({
        card: 'generic',
        kind: 'search',
        title: `知识库状态：${args.workspace}`,
      }),
    }))
  }

  ctx.effect(() => {
    let disposed = false
    register().catch((error) => {
      ctx.logger?.warn?.(`kb-ingest: tool registration failed: ${error?.message ?? error}`)
    })
    return () => { disposed = true; void disposed }
  })
}

export default { name, inject, apply }
