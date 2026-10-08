# dsh-english-pdf-kb

English PDF ingestion tools packaged as a DSH/Cordis bundle. This shell invokes
`english_ingest.py` in the translation-agent checkout. Tool registration alone
does not prove that a backend stage or a real-book quality gate has passed.

| Tool | Arguments |
| --- | --- |
| `en_pdf_prepare` | `source`, `workspace`, `title`, `author`, optional `config` |
| `en_pdf_status` | `workspace` |
| `en_pdf_run` | `workspace`, `stage`, optional `pages`, `plan_file`, `toc_file`, `toc_pages`, `config`, `layout_review_file`, `embedding_mode`, `timeout_seconds` |

Stages: `extract`, `ocr`, `toc`, `translate`, `plan`, `draft`, `publish`, `verify`,
`register`. Both `extract` and `ocr` require a nonempty list of unique positive
integer PDF page numbers. Page order is preserved; discrete pages never expand
into a range. `translate` may omit pages. When supplied, `toc_pages` follows the
same exact-page validation. `config` is a path to an existing pipeline TOML file,
never a configuration object or an API key. `embedding_mode` accepts `off`
(lexical only), `auto` (provider when configured), or `on` (vector index required).
Backend evidence and gate results determine stage completion.

## Installation

In DSH use `plugin_manager install_bundle <absolute-directory-path>`, or with the
standalone CLI use `dsh plugin --profile desktop add <absolute-directory-path>`.
Reload existing sessions after the host installs the bundle. This bundle depends
on the translation-agent checkout and its Python dependencies; it is not a
standalone document processor.

The Python interpreter resolves from plugin `python`, `DSH_ENGLISH_PDF_PYTHON`,
`DSH_KB_PYTHON`, repository `.venv`, then PATH `python`. Plugin `repoRoot` or
`DSH_KB_REPO_ROOT` sets the working directory. Plugin `script` can override the
backend path. `contract: false` disables the system prompt section.

The shell launches Python without a shell or visible Windows console and sends
UTF-8 stdin JSON `{ "command": "prepare|status|run", "args": { ... } }` with
`--stdin-json`. Backend stdout must contain exactly one JSON object. Structured
backend failures are returned to the caller. Backend stderr and malformed stdout
are not copied into tool errors, to avoid disclosing diagnostic secrets. Public
argument names are whitelisted. Credentials belong in the environment or a
protected configuration file and must never be supplied as tool arguments.

`timeout_seconds` is an integer from 1 through 3600 (default 1800), enforced by
the shell. Cancellation and timeout terminate only the spawned process tree.
Daemon-side jobs, such as Docker containers, require backend-owned cleanup;
inspect workspace status before resuming an interrupted operation. Tools are
marked non-concurrent because stages may update the same workspace.

## Shell verification

```text
node tools/english_pdf_kb_plugin/test/plugin.test.mjs
```

Tests use a Python stub and cover registration, input schemas, UTF-8 JSON,
whitelisting, exact page handling, structured failures, diagnostic redaction,
timeout, cancellation, and owned descendant cleanup. They do not validate live
OCR, translation, embeddings, or rendered document quality.

## Backend and content plan

The project backend is english_ingest.py. prepare binds an independent workspace
to the source PDF hash, verified Chinese title, editor/author and page count.
extract imports embedded English text and original block rectangles into
immutable page checkpoints; low-text pages are reported for exact-page OCR.
ocr, toc and translate call the existing book_pipeline.py phases. toc_file
imports a reviewed TOC without a model call, while toc_pages selects exact
source pages for automatic TOC generation.

The reviewed plan is documented by the project skill
skills/english-pdf-kb/references/tool-contract.md. It binds source and TOC
hashes, classifies every TOC ID as selection, editorial or structure, supplies
Chinese reader titles and source reviews, and records exact shared-page clips
or reviewed chapter overrides. The original page checkpoints remain intact.
draft runs the fast chapter gate. publish builds a content-addressed candidate
with selected-text Markdown, five-field Chinese KB, Word, EPUB, reference PDF,
editorial index, clip ledger, note census and lexical RAG sidecar. A repeated
publish with unchanged source, plan, page translations, reviewed chapters and
publisher code reuses that candidate.

verify runs the full publication gate and writes a checksum-bound receipt only
on a release-ready report. register requires that current receipt plus a real
Word layout review bound to the DOCX hash, then registers one book. The plugin
does not install itself, bypass the Chinese language gate, or sync the global
repository-wide index.

## Backend verification

    python -m unittest tests.test_english_pdf_ingest tests.test_project_skills
    node tools/english_pdf_kb_plugin/test/plugin.test.mjs

The Python tests use a synthetic English PDF to cover source binding, exact
text-layer extraction, reviewed TOC import, shared-page clipping, editorial
exclusion, Word/EPUB/PDF publication, all full release checks, lexical
registration and stale-artifact detection. They do not call live OCR,
translation or embedding services.
