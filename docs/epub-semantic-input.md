# EPUB 文档语义层与 first-class DAG

born-digital EPUB 不运行 OCR，也不伪造 PDF 页、目录或分页检查点。推荐产品入口
`translation-agent run --source-mode epub` 会把源 ZIP 安全检查、OPF spine 语义
重建、单元翻译/回填、reader 物化、出版和 native release gate 组织为真实 Graph
目标闭包。`epub_semantic_import.py` 与 `semantic_translation_runner.py` 仍保留为
可检查、可离线回填的细粒度工具。

## 1. 完整 Graph 运行

先查看真实计划；此操作会读取并安全检查源 EPUB，但不会调用模型或生成出版物：

```bash
translation-agent plan "book.epub" -o "outputs/book" \
  --source-mode epub \
  --target publication.epub_report \
  --config pipeline.toml \
  --translation-profile deepseek_flash
```

默认执行翻译并运行 EPUB-native 正式发布门：

```bash
DEEPSEEK_API_KEY=... translation-agent run "book.epub" \
  -o "outputs/book" \
  --source-mode epub \
  --config pipeline.toml \
  --translation-profile deepseek_flash \
  --target-language 简体中文
```

当 `verify=true` 且未显式给 target 时，EPUB 默认目标是
`publication.epub_report`；成功报告位于
`audit/epub-release-report.json`。只有报告、Graph state 与 EPUB 成品哈希一致，
CLI/WebUI 才能返回或显示 `release_ready=true`。正式报告固定声明 `mode=full`、
`publication_profile=epub` 和 `verifier_node=core.publication.verify.epub`，不能用
PDF 的 `release-report.json` 或 Word report 冒充。

不翻译源语言内容时使用 `--no-translate`，并把 `--target-language` 设置为成品实际
语言。例如英文源书：

```bash
translation-agent run "book.epub" -o "outputs/book" \
  --source-mode epub --no-translate --target-language en
```

只有明确关闭质量门时才生成草稿：

```bash
translation-agent run "book.epub" -o "outputs/book" \
  --source-mode epub --no-verify
```

此时空 targets 规范化为 `publication.epub` 与 `publication.docx`。裸 EPUB/Word
都不是正式交付证据；EPUB 来源的 Word 目前没有独立 OOXML/render release profile。

## 2. DAG 节点与 targets

正常翻译链为：

```text
core.source.epub.inspect
  → core.reconstruct.epub_semantic
  → core.semantic.translate
  → core.semantic.apply
  → core.publish.epub
  → core.publication.verify.epub
```

`--no-translate` 时以 `core.semantic.materialize_reader` 取代 translate/apply；程序化
`translation_mode=apply` 则以 `core.semantic.translations.inspect` 绑定外部 JSONL，
再进入 `core.semantic.apply`。Word 从同一个 `chapters.reader` 分支生成，不会另读
可能陈旧的 canonical 章节。

EPUB Graph 支持以下 target artifacts：

| target | 含义 |
| --- | --- |
| `source.epub` | 已绑定 SHA-256 与 ZIP 预算的源文件身份 |
| `chapters.semantic` | 不可变 source chapters 与 reconstruction audit |
| `semantic.translation_units` | canonical 翻译单元 JSONL |
| `semantic.translations` | 在线生成或外部绑定的完整翻译集合 |
| `chapters.reader` | 通过结构/语言/术语复验的唯一 reader bundle |
| `publication.epub` | 未单独验收的 EPUB 文件 |
| `publication.docx` | 未单独验收的 Word 文件 |
| `publication.epub_report` | EPUB-native 正式发布报告；自动依赖 `publication.epub` |

EPUB 暂不支持知识库、参考 PDF、`publication.report` 或
`publication.word_report`。EPUB Graph 也尚未消费 Recipe TOML；传入 `--recipe`
会在计划阶段失败，不会静默忽略。需要收窄运行时直接使用一个或多个 `--target`。

Python API：

```python
from pathlib import Path
from pipeline_graph.epub import EpubGraphOptions, run_epub_graph

result = run_epub_graph(
    Path("book.epub"),
    Path("outputs/book"),
    options=EpubGraphOptions(
        translation_mode="none",
        target_language="en",
        target_artifacts=frozenset({"publication.epub_report"}),
    ),
)
```

`translation_mode=run` 的调用方必须显式注入请求 callback 和不含密钥的请求身份
fingerprint；Graph 模块本身不加载 `.env`。源 EPUB 和外部 translations 文件会在
规划时建立精确快照，执行前若字节已变化则失败，不复用旧缓存。

## 3. Canonical TranslationUnit

EPUB 与 `born_digital_pdf_import.py` 的当前 writer 都输出八个固定顶层字段：

```json
{
  "schema_version": 1,
  "id": "chapter-0001-u0001-...",
  "chapter_id": "chapter-0001",
  "sequence": 1,
  "kind": "paragraph",
  "source_markdown": "Source paragraph.",
  "source_sha256": "<sha256-of-source_markdown>",
  "locators": [
    {
      "adapter": "epub",
      "source": "OEBPS/chapter.xhtml",
      "page": null,
      "href": "OEBPS/chapter.xhtml",
      "anchor": null,
      "block_index": 0
    }
  ]
}
```

`sequence` 从 1 连续递增，`block_index` 从 0 计数；`source_sha256` 必须与
`source_markdown` 精确匹配。`kind` 使用 canonical 枚举，例如 `heading`、
`paragraph`、`list_item`、`table`、`footnote_definition`。EPUB locator 记录源
spine `href`；它只用于审计和错误定位，不得泄漏进读者出版物。

strict `TranslationUnit.from_dict()` 只接受 canonical 形状。为恢复既有 schema-v1
检查点，runner/apply 的输入边界会兼容读取：

- EPUB 旧字段 `source_href` → 一个 `adapter=epub` locator；
- 文字 PDF 旧字段 `source_pages` → 一个 `adapter=text-pdf` locator；
- 旧 `kind=list` → canonical `list_item`。

兼容层拒绝 canonical `locators` 与旧 locator 字段混写，也拒绝未知字段、陈旧源
哈希和无 locator 单元。所有当前 writer 只写 canonical 形状，因此旧字段不会继续
传播。

## 4. 独立导入、准备与回填

需要人工检查或离线提供译文时，可以拆开运行：

```bash
python epub_semantic_import.py import "book.epub" -o "work/book-semantic"
```

主要产物：

- `chapters.json` 与 `chapters/*.md`：导入后的 canonical 章节；
- `semantic/source_chapters/*.md`：不可变源语言语义基准；
- `semantic/translation-units.jsonl`：canonical 细粒度翻译单元；
- `audit/semantic-reconstruction.json`：spine、脚注闭环、源身份和阻断项审计。

匿名脚注元素只在紧跟某个带 ID 脚注时作为续段合并；孤儿匿名脚注阻断发布。
printed-page/index locator 只保留可见文本，不把源 EPUB 文件坐标发布给读者。

`prepare` 只生成模型提示，不联网：

```bash
python semantic_translation_runner.py prepare \
  work/book-semantic/semantic/translation-units.jsonl \
  -o work/book-semantic/semantic/prepared.jsonl \
  --glossary glossary.json \
  --config pipeline.example.toml \
  --translation-profile deepseek_flash
```

显式运行模型时，API key 只从 Profile 指定的环境变量读取；缓存键包含源文本、
provider/endpoint/model、prompt contract、目标语言和术语表：

```bash
DEEPSEEK_API_KEY=... python semantic_translation_runner.py run \
  work/book-semantic/semantic/translation-units.jsonl \
  -o work/book-semantic/semantic/translations.jsonl \
  --glossary glossary.json \
  --config pipeline.example.toml \
  --translation-profile deepseek_flash \
  --max-chars 9000 --concurrency 16 --retries 3 \
  --cache-dir work/book-semantic/.translation-cache
```

脚注、内联结构和 `UNIT` 边界使用顺序敏感保护 token；少传、多传、重排、模型
前言、结构漂移、异常长度或目标语言失败时不写完整翻译文件。回填再次核对单元
集合、源 SHA、章节顺序、链接目标与脚注闭环，并以 staging + rollback 提交：

```bash
python epub_semantic_import.py apply-translations \
  -o work/book-semantic \
  work/book-semantic/semantic/translations.jsonl
```

这些细粒度命令便于审查，但不能单独签发 native release report；正式交付应使用
完整 Graph `run`，或程序化调用 `verify_epub_publication()` 绑定同一源、语义审计、
canonical chapters 和具体 EPUB artifact。

## 5. EPUB-native 发布门与已知边界

`core.publication.verify.epub` 不调用模型，依次执行八个检查：

1. 源路径/SHA-256 与 reconstruction audit 身份；
2. reconstruction audit、不可变源章节和脚注摘要；
3. translation audit 与上游审计/当前 reader 的哈希绑定；
4. canonical manifest、章节顺序、可见文本与脚注闭环；
5. EPUB3 mimetype/container/OPF/manifest/spine/语言/资源清单；
6. nav 顺序、标题和 fragment；
7. XHTML 正文、脚注数及所有 `href`/`src` 内部目标；
8. 验收期间未变化的成品 SHA-256 与大小。

ZIP 路径穿越、重复/加密/symlink 成员、超出单成员或总解压预算、坏 CRC 都会
fail closed。外部 `http`、`https`、`mailto` 超链接可以保留；外部图片/媒体资源
不允许，`src` 必须指向包内 manifest 资源，内部 fragment 必须真实存在。

当前路径仍是 text-first，限制必须显式理解：

- importer 会把 `<img>` 表示为 Markdown，但 publisher 尚未建立完整的源图片二进制
  复制、媒体类型登记和路径重写管线；含图片的书可能无法通过 native gate；
- 普通跨-spine 链接会通过两遍 manifest 映射改写为新章节文件名；被引用的源
  fragment 会生成稳定发布锚点，并由 native gate 对目标文件和 fragment 双重闭合；
- 指向非 spine 资源的复杂链接仍依赖后续资产管线，无法证明闭合时会被 native gate
  阻断；
- native gate 是结构/内容/身份门，不是视觉渲染门；它不验证阅读器矩阵、图片像素、
  alt 文本质量、字体或分页外观。

这些限制不能通过关闭检查来获得“正式”状态。确需保留复杂图片和非 spine 资源的
书，应先补齐资产复制与链接重写，再运行 native gate；`--no-verify` 只代表草稿。
