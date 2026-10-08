# 保真门判读

两道门串行：**中文语言门**先跑（决定是否写盘），**Word 保真门**后跑（决定是否合格）。
两份报告分别落在 `<工作区>/audit/kb-ingest-report.json` 与
`<工作区>/audit/word-fidelity-report.json`。所有数字都是本次运行重新计算的，
不得复用另一本书的历史值。

## 中文语言门

判据复用 `kb_translation.classify_row`，与 `translation-agent-kb register`、
`publication_verifier` 的 `knowledge_base.chinese` 与全库 `sync` 是同一个判据。

- 命中即 `blocked`，**语料不写盘**。返回 `pending_count`、`languages` 与样本。
- 豁免（`exempt`）在报告里逐类计数，常见为 `already_chinese`、`reference_material`、
  `not_prose`。豁免不等于门通过，只是这些块不需要翻译。
- `allow_foreign` 是唯一逃生口。用它就要在交付里写明"本库含未译外文块"。

## Word 保真门

### 三个核心数字

| 字段 | 含义 | 判定 |
| --- | --- | --- |
| `missing_characters` | 源文件有、Word 没有的正文字符数 | 必须为 0 |
| `extra_characters` | Word 有、源文件没有的正文字符数 | 必须为 0 |
| `similarity` | 有序对齐相似度 | 必须为 1.0 |

这三个数字在**去掉全部空白**后比较。原因：Markdown 把一个段落边界渲染成两个空格、
Word 渲染成一个空格，这是版面差异而非内容缺失；把它报成"缺 1 字"会让报告失去可信度。
空白差异单独记在 `whitespace_only_missing` / `whitespace_only_extra`，并且**只降级为
警告，不会让门变红**。原文的 `raw_missing_characters` 仍保留在 `metrics` 里供审计。

对齐用 `difflib.SequenceMatcher`（`autojunk=False`）而不是字符多重集：多重集看不见
乱序，也说不出差异发生在哪里。`hunks` 给出每一处的 `kind`、缺文、多文与上下文，
最多 40 条（`hunks_truncated` 标记截断），但计数始终精确。

### issue 代码

| 代码 | 含义 | 处置 |
| --- | --- | --- |
| `docx_text_mismatch` | 正文字符不一致 | 按 `hunks` 回源文件修；**不要改 Word** |
| `docx_heading_mismatch` | Heading 1 章节标题与源不一致 | 检查源文件标题层级 |
| `docx_line_break_present` | 段落内有 `w:br`/`w:cr` 硬换行 | 源文件里的 OCR 硬换行未被合并；见下 |
| `docx_sentence_split` | 段落在句子中间被切断，下段以标点开头 | 同上，属于可读性缺陷 |
| `docx_source_metadata_stripped` | 发布器删掉了源文件里的页码/书眉行 | 清理源文件后重跑，或确认删除是有意为之 |
| `docx_trace_*` | 检出模型前言、`TRANSLATION_FAILED`、页码标记、替换字符等发布痕迹 | 回源文件清除 |

`docx_trace_*` 是 `publication_verifier.TRACE_PATTERNS` 的五类：`source_page_marker`、
`decorated_page_number`、`internal_placeholder`、`model_preamble`、
`replacement_character`。它们证明"没有编造和没有半成品"，与字符计数互补。

### warning 代码

| 代码 | 含义 |
| --- | --- |
| `docx_whitespace_only_difference` | 只差空白折叠；正文一致，不需要处理 |
| `docx_source_page_markers_present` | 源文件含 `<!-- PDF_PAGE: n -->` 或独立页码；渲染时会被移除，不影响正文 |
| `docx_core_title_mismatch` | 文档属性标题与本书记录的书名不一致 |

## 反常换行的判读

`line_break_count` 统计 `word/document.xml` 里所有非分页的 `<w:br/>` 与 `<w:cr/>`。
这是本仓库此前**没有任何验收器检查**的一项（`docx.structure` 只看
`w:type="page"` 的分页符），而
`skills/docx-publication-finisher/references/acceptance-gates.md` 明确要求
"非分页普通 `w:br` 为零"。

- 计数为 0 是正常目标。发布器会把 OCR 视觉换行合并（`_join_wrapped_lines`：
  只有两侧都是 ASCII 字母数字时才补空格，中文直接相连），所以中文源文件正常
  得到 0。
- 计数大于 0 说明源文件里的换行**没有被当作软换行处理**。常见原因：源文件把每一行
  都写成独立段落（空行分隔），或源里有显式 `<br>`。
- `allow_line_breaks` 只把该项降级为警告，**仅当源文件本身是诗歌、歌词或列表时使用**。
  它不是"让门变绿"的开关；用它要在交付里说明理由。

`docx_sentence_split` 更严格：它只在"上一段没有句末标点，且下一段以收尾或连接标点
开头"时触发。散文永远不会有这种排列，所以该信号不会误伤标题、列表、对话与短句。

## 门通过 ≠ 内容为真

保真门证明的是 `Word == 源文件`。它**不**证明 `源文件 == 原书`。如果源文件本身来自
模型生成、OCR 或人工转录，这一层不确定性必须单独向用户说明。同理，跨书检索的
Hit@k 衡量的是检索定位，不代表原书 OCR、翻译或校对已经通过质量门。

## 已验证基准（仅作校准证据）

促成本 skill 的一次运行：中文 Markdown 源 2 章 690 字符，产出 Word 缺字 0、多字 0、
相似度 1.0，段内硬换行 0；同一源加入 OCR 硬换行与独立页码后，发布器把硬换行合并为 0、
把页码作为 `docx_source_page_markers_present` 显式报出而非静默丢弃；另一次运行在正文
注入 `TRANSLATION_FAILED` 后由 `docx_trace_internal_placeholder` 拦下。未来运行必须
重新计算这些数字。
