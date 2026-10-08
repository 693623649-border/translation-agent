# dsh-kb-ingest — 源文件入库与中文 Word 保真交付

把一个源文件变成「可检索的知识库 + 逐字校对通过的中文 Word」。

这是本仓库数据管线的**第四个入口**。README 的管线总览里已有三条路径
（扫描 PDF 走 OCR、文字 PDF/EPUB 走语义导入、粘贴文走 `txt_article_kb`），
它们都汇聚到 `knowledge_base.jsonl`；这个插件补的是"任意源文件一步到位，
并且证明产出与源文件逐字一致"。

## 三个工具

| 工具 | 作用 |
| --- | --- |
| `kb_ingest_source` | 源文件 → 五字段语料 + 路由侧表 + RAG 清单 + 中文 Word，并跑两道门 |
| `kb_verify_word` | 对已有工作区独立重跑逐字保真门（往返校验，不复用上次结论） |
| `kb_ingest_status` | 语料规模、侧表覆盖率、中文门与保真门的当前状态 |

## 它解决了什么

`translation-agent-kb derive-docx` 是最接近的既有命令，但它读 DOCX、只写语料，
**从不检查页面上出现的文字是否就是源文件的文字**。仓库里确实有一个逐字比较
（`publication_verifier` 的 `docx_chapter_text_mismatch`），但它是二值的：只报两个
SHA-256 和两个字符数——运营者知道少了 3 个字，却不知道是哪 3 个。而且它的期望值来自
流水线输出目录里的 `chapters/*.md`，一个独立的源文件没有这些东西。

本插件复用同一条归一化链（`_docx_markdown_body` → `_normalize_wrapped_markdown_for_docx`
→ `_markdown_visible_text` → `_canonical_visible_text`），因此它的判定不会与发布门漂移，
并补上三件此前**全仓库都不存在**的东西：

1. **`missing` / `extra` 的逐处定位。** 有序字符对齐（`difflib.SequenceMatcher`，
   `autojunk=False`）给出精确计数、相似度与有界的 diff 块。多重集比较看不见乱序，
   也无法说明差异发生在哪里；改写的段落在这里显示为同一偏移上的一对删/插，而不是
   两个互不相干的计数变化。
2. **段内硬换行检查。** 仓库里**没有任何验收器**统计普通的 `<w:br/>`——
   `docx.structure` 只看 `w:type="page"`。而
   `skills/docx-publication-finisher/references/acceptance-gates.md` 明确要求
   "非分页普通 `w:br` 为零"。本插件逐个统计、定位，并区分"是否切断了句子"。
3. **发布痕迹检查。** 把 `publication_verifier.TRACE_PATTERNS` 重新施加到 Word 正文上，
   使模型前言、`TRANSLATION_FAILED`、来源页码标记与替换字符无法冒充正文。

## 安装

作为 DSH bundle 装入当前 profile：

```
plugin_manager install_bundle  <本目录>
```

或把 `tools/kb_ingest_plugin` 链接进 profile 后，在其 `cordis.patch.yml` 中 insert
`dsh-kb-ingest`（本目录的 `cordis.patch.yml` 已给出可直接使用的默认配置）。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `python` | `null` | 解释器；留空则探测 `<repo>/.venv`，再回退 PATH 上的 `python` |
| `ingestTimeoutMs` | `900000` | 单本书的发布 + 校验上限 |
| `verifyTimeoutMs` | `300000` | 重新校验上限 |
| `statusTimeoutMs` | `60000` | 状态查询上限 |
| `defaultChunkChars` | `4000` | 段落切块上限，与仓库契约一致 |
| `contract` | `true` | 是否注入"必须走工具、不许手写脚本"的系统提示契约 |

环境变量覆盖：`DSH_KB_PYTHON`、`DSH_KB_INGEST_SCRIPT`、`DSH_KB_REPO_ROOT`。

## 命令行（不经 DSH 也可用）

```bash
python tools/kb_ingest_plugin/kb_ingest.py ingest "book.md" \
    --output-dir "outputs/我的书" --title "书名" --author "作者"

python tools/kb_ingest_plugin/kb_ingest.py verify-word "outputs/我的书"
python tools/kb_ingest_plugin/kb_ingest.py status "outputs/我的书"
```

每个命令在 stdout 上输出**恰好一个** JSON 文档；域失败（语言门、保真门）退出码为 1
但 JSON 完整，便于流水线在不解析输出的情况下止损。

## 三种判定

| `status` | 含义 | 语料是否写盘 |
| --- | --- | --- |
| `passed` | 两道门都通过 | 是 |
| `blocked` | 中文语言门拦下 | **否** |
| `failed` | 保真门未通过 | 是（产物在盘上但不合格） |

保真门只在**正文字符**上判定（比较前去掉空白）。Markdown 把一个段落边界渲染成两个
空格、Word 渲染成一个空格，属于版面差异而非内容缺失；这类差异记在
`whitespace_only_missing` / `whitespace_only_extra` 并**只降级为警告**。原始计数仍在
`metrics.raw_missing_characters` 里可供审计。

## 验证

```bash
python -m unittest tests.test_kb_ingest          # 49 个用例
# 插件测试必须从 DSH profile 目录运行，否则 `@deepseek-ai/dsh-tools` 无法解析：
(cd "$HOME/.dsh/profiles/desktop" && node "E:/Deeplearning/translation-agent/tools/kb_ingest_plugin/test/plugin.test.mjs")
```

`plugin.test.mjs` 的 40 个断言刻意包含**应当失败**的输入（外文语料、损坏 PDF、缺字/多字
注入、硬换行注入），因为一个永远报成功的门比没有门更糟。它还逐字段断言每个工具的返回值
可以无损通过 JSON 往返——注册器会对整个结果做同样的快照，只要有一个 `undefined` 属性，
工具就会注册成功、单测全绿，然后在每次真实调用时失败。

### EPUB 的标题处理（易错点）

EPUB 文档里的 `<h1>`–`<h6>` 会被单独取出当作章节名，并**从正文中移除**。若不移除：
标题被写两次，而且发布器的"书眉剥离"（`strip_publication_metadata` 的 running-title
启发式）会把与标题相似的那一行当书眉删掉，于是**真正的一行正文被静默删除**。移除只
发生在正文**开头**的连续等值段落上，每个标题只消费一次，所以正文中段真正重复标题的
句子不会被误删。回归测试见
`tests/test_kb_ingest.py::SourceAdapterTests::test_epub_heading_is_not_duplicated_into_the_body`
与 `test_heading_dropping_only_consumes_a_leading_run`。

## 三个陷阱（已在代码中处理）

### 1. stdin/stdout 编码（Windows）

Python 在 Windows 上按控制台代码页决定 stdio 文本编码，于是携带中文路径或书名的
UTF-8 JSON 请求会被按 GBK 解码，变成代理字符并在输出时抛
`UnicodeEncodeError: surrogates not allowed`。插件在 spawn 时显式设置
`PYTHONIOENCODING=utf-8` 与 `PYTHONUTF8=1`，使这座桥与代码页无关。

### 2. argv 编码（Windows）

中文书名若走命令行参数会被控制台代码页转换破坏，因此请求一律走 stdin 的
`--stdin-json`，不走 argv。

### 3. PyMuPDF 在拒绝坏文件时泄漏文件句柄

`pymupdf.open(<路径>)` 在文件格式非法时，**其构造函数自身**会先打开句柄再抛错，
调用方拿不到可关闭的文档对象，于是在 Windows 上源文件被锁住、无法移动或删除
（表现为 `PermissionError: [WinError 32]`）。修法是本模块自己读字节并交给
`pymupdf.open(stream=..., filetype="pdf")`，句柄始终由本模块掌握，坏文件不会留下锁。

## 相关

- 工作流与判读规则：`skills/source-to-kb-word-workflow/SKILL.md`
- 门代码表：`skills/source-to-kb-word-workflow/references/fidelity-gates.md`
- 适配器与产物契约：`skills/source-to-kb-word-workflow/references/source-adapters.md`
- 成品 Word 的排版修复：`skills/docx-publication-finisher/SKILL.md`（本插件不修成品，只管入库与保真）
