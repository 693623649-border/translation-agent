# 源适配器与产物契约

## 五个适配器

| 后缀 | 适配器 | 章节切分依据 | 风险 |
| --- | --- | --- | --- |
| `.md` / `.markdown` | `markdown-headings` | 最浅的 ATX 标题层级 | 代码围栏内的 `#` 不会被当成标题 |
| `.txt` / `.text` | `plaintext-headings` | 独立的编号/章节标题短行 | 超过 60 字符或以句末标点结尾的行不算标题 |
| `.docx` | `docx-headings` | `Heading N` 段落 | 没有标题结构直接失败，不会退化成单章 |
| `.epub` | `epub-spine` | OPF `spine` 顺序 | 无法确定阅读顺序即失败，不猜 |
| `.pdf` | `pdf-text-layer` | 文字层中的编号标题行 | 文字层不足 200 字符判定为扫描件并拒绝 |

扫描件、无 spine 的 EPUB、无标题的 DOCX 一律 **fail-closed**：失败信息会给出真正的
入口而不是产出一份不可审计的语料。

### EPUB 的标题处理（易错点）

EPUB 文档里的 `<h1>`–`<h6>` 会被单独取出当作章节名，并**从正文中移除**。若不移除：
标题被写两次，而且发布器的"书眉剥离"启发式会把与标题相似的那一行当书眉删掉，于是
真正的一行正文被静默删除。移除只发生在正文**开头**的连续等值段落上，每个标题只消费
一次，所以正文中段真正重复标题的句子不会被误删。回归测试见
`tests/test_kb_ingest.py::SourceAdapterTests::test_epub_heading_is_not_duplicated_into_the_body`
与 `test_heading_dropping_only_consumes_a_leading_run`。

### 各入口的命令

- 扫描 PDF → `python graph_pipeline.py <pdf> -o <输出目录> --phase all --config pipeline.toml --recipe recipes/chinese-pdf-word.toml`
- 结构复杂的 EPUB → `python epub_semantic_import.py`
- 只有 DOCX 没有上游语义源 → `translation-agent-kb derive-docx`，或
  `python docx_semantic_migration.py` 建立可审计的 reviewed source

## 五字段语料契约

`knowledge_base.jsonl` 每行恰好五个字段，与全库检索、验收器、向量索引共用：

```json
{"id": "<sha1 40 位>", "title": "[书名] 章节标题", "chapter_id": "01_书名:章节-slug", "chapter_order": 1, "content": "..."}
```

- `content` 按段落边界切成 ≤4000 字符的块，与 `book_pipeline.split_text` 同契约。
- `id` 由 `源文件名:chapter_id:chunk_index` 决定，**与块内容无关**。因此源文件不变时
  重跑得到逐字节相同的 ID，下游 embedding 缓存与全库索引不会失效；这也意味着
  **改了正文就必须重跑 ingest**，不能只改源文件。
- 单行超长段落仍会在段内硬切，与主流水线一致。

## 随语料一起产出的侧车

| 文件 | 作用 |
| --- | --- |
| `knowledge_base.meta.jsonl` | 路由侧表：`book_id` / `book_title` / `author` / `language` / `source_path`。检索按书、按作者、按语言过滤都靠它，覆盖率必须为 1.0 |
| `knowledge_base.rag.json` | RAG 清单，`initialize_rag_manifest` 生成；正式 hybrid 要求有效 ready 向量索引，否则明确报告不可用 |
| `knowledge_base.apparatus.json` | 装置标注与默认权重（目录/索引/版权页 0.25，说明性装置 0.7，正文 1.0），由 `rag_apparatus` 生成 |
| `chapters.json` | 章节清单，含发布器实际渲染的 `published_markdown`，是 `kb_verify_word` 的比对依据 |
| `chapters/*.md` | 逐章 Markdown，审计事实源 |
| `audit/kb-ingest-report.json` | 入库报告：源哈希、适配器、门结果、产物路径 |
| `audit/word-fidelity-report.json` | 保真报告：缺字/多字/相似度、hunks、换行与痕迹统计 |

## Word 版面契约

版面全部由 `book_pipeline.build_docx` 决定，不得替换：

- A4（21×29.7cm），页边距 上2.35 / 下2.25 / 左2.55 / 右2.55 cm
- 封面：书名（`Codex Book Title`）+ 可选作者（居中 `Normal`）+ 分页符
- 正文：SimSun 11pt、两端对齐、首行缩进 22pt、行距 1.5、孤行控制
- `Heading 1` 分页起始、Microsoft YaHei 18pt 加粗；`Heading 2/3` 12–14pt
- 页脚居中页码（首页不同）；真实脚注使用 `word/footnotes.xml`
- 段内换行只应来自作者显式的 `<br>`；OCR 软换行必须在发布前合并

## 入库后的检索

```bash
# 单书
translation-agent-kb status "<工作区>"
translation-agent-kb retrieve "<工作区>" "<问题>" --mode hybrid

# 全库（跨书）
python global_knowledge_base.py sync
python tools/kb_qa_plugin/kb_qa.py ask "<问题>" --mode hybrid --limit 5
```

`sync` 默认拒绝未译外文 reader。正式检索统一 hybrid（BM25 + 向量 RRF），
向量、provider 或 embedding 配置不可用时明确失败，不以自动 BM25 降级取代。
`global_knowledge_base.py search` 仍是本地 FTS 候选发现/诊断接口；正式跨书
取证通过 `kb_qa.py ask --mode hybrid` 完成精读。
