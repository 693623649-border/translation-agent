# 译文省略号（……）缺陷：诊断与处置

## 症状

发布的 Word 里出现原文没有的省略号，把整句、整段内容吞掉，读起来像"跳过了"。
典型形态：

```
……两者共同构成了……
……留下了一种持久的……
……最终走向……
```

严重时一句里出现多个 `……`，句子被切碎成若干残片。

## 性质：提示词违反，不是排版问题

页面翻译的提示词（`book_pipeline.py` 第 4 条）已经明确禁止：

> 严禁用省略号（……）概括、省略或跳过任何内容——原文没有省略号的地方绝不允许出现省略号，
> 原文自带的省略号原样保留。

所以 `……` 出现在原文没有省略号的位置，**是模型违反了硬性指令**：它读不懂某段 OCR
（缺字、形近误字、句子交错），于是用省略号把读不懂的部分一笔带过，而不是按上下文推断复原。
这是**内容缺失**，不是格式问题；`docs` 里那句"缺字漏字"指的正是这件事。

第 5 条同时禁止 `[原文存疑]` / `[存疑]` 之类的存疑标注，只允许 `（原文缺损）`。

## 判据（可靠、可离线、可复现）

对每一页比较 OCR 原文与译文里的省略号**数量**：

```
excess = 译文省略号对数 - 原文省略号对数
excess > 0  →  该页有原文没有的省略号
```

为什么用数量差而不是"出现即判负"：对话、目录引导点、欲言又止的句子都会**合法**使用
`……`。原文自带的省略号必须原样保留，所以只能判"多出来的"。这个判据不需要模型、
不需要网络，逐页可复核。

计数时 `…` 与 `……` 视为同一种证据（用 `…+` 归并），否则"原文 `…` 译文 `……`"会被
误判为干净。少于 30 字的页面（图版、空白页）不参与判定。

## 现状实测（本仓库 outputs/）

对 9 个带 `pages/` 的工作区、513 个有译文的页面做全量普查：

| 工作区 | 违规页 | 凭空多出的省略号对 | 存疑标注页 |
| --- | --- | --- | --- |
| 私小説論 | 92 | 667 | 5 |
| The Downward Spiral | 22 | 26 | 0 |
| 其余 7 个工作区 | 0 | 0 | 0 |

最严重的单页：`私小説論` 第 366 页，**原文 0 个省略号，译文 42 个**。

两个结论：

1. 缺陷集中在**经过页面级翻译**的书上，且与 OCR 质量直接相关（`私小説論` 是扫描件）。
2. 它此前**完全没有门禁**：`publication_verifier.py` 里 `省略`、`存疑`、`……`、
   `ellipsis` 的出现次数全是 0。`The Downward Spiral` 在 22 页违规的情况下，
   发布报告仍是 `status=passed / release_ready=True`。

## 已建立的处置

### 1. 新增门禁 `translation.ellipsis`

`publication_verifier._check_translation_ellipsis`，在 `full_checks` 中注册，
**默认必需**（不带 `require_*` 开关）。逐页比较 `text` 与 `translated_text`，产出：

- issue `translation_ellipsis_invented`：证据含 `affected_pages`、
  `invented_ellipsis_pairs`、`samples`（每页的原文/译文省略号计数）
- issue `translation_doubt_marker_present`：译文里出现 `[原文存疑]` / `[存疑]` /
   `原文即此` / `（原文缺损）`
- metrics：`pages_checked`、`pages_with_invented_ellipsis`、
  `invented_ellipsis_pairs`、`pages_with_doubt_markers`

无 `pages/` 目录（EPUB 原生或 Word-only 工作区）时该检查静默通过，
在 `publication_profile="word"` 下允许跳过，不会给非 PDF 流水线制造假失败。

### 2. 修掉自相矛盾的校勘提示词

`book_pipeline.proofread` 第 5 条原本要求模型"无法可靠还原的文字……紧邻标注
`[原文存疑]`"，而翻译提示词第 5 条明令禁止输出该标注。校勘阶段产出的 `[原文存疑]`
会被原样带进译文，直接违反下游契约。现已改为 `（原文缺损）` 并显式说明禁止存疑标注。

### 3. 修复手段（已有，可复用）

`tools/books/shishosetsu_page_retranslate.py` 是为此写的一次性修复脚本，其
`page_needs_fix` 用的正是同一个判据。它做了三件关键的事，重跑任何一本书都应照做：

- **带上一页页尾作为只读上下文**，让跨页断句的页面顺势续译，而不是因为"这句话不完整"
  就省略；
- **注入严格的反省略指令**（该脚本的 `PAGE_DIRECTIVE`）；
- **逐页写盘 + 写前自检**：译文仍有多余省略号就拒收该页，不覆盖已有内容。

`tools/books/retranslate_docx_paragraphs.py` 是同一思路的段落级版本。

## 修复流程

```bash
# 1. 先看有哪些页面违规（只读，不写盘）
python -m unittest tests.test_publication_verifier -k ellipsis   # 门禁自身的回归

# 2. 用带反省略指令 + 上页上下文的方式重译违规页，逐页写盘
#    （照 tools/books/shishosetsu_page_retranslate.py 改 ROOT 指向目标工作区）

# 3. 重跑编译与门禁
python book_pipeline.py "<源>.pdf" -o "outputs/<工作区>" --phase compile
```

## 不要做的事

- **不要全局删除 `……`。** 原文自带的省略号（对话、引导点、欲言又止）必须保留；
  按外观批量删除会破坏正文。只能按"原文没有而译文有"逐页定位。
- **不要把门调松。** `translation.ellipsis` 是默认必需的检查；把它加进 `disable`
  或放宽判据只会让缺字继续流到成品。
- **不要指望结构门发现它。** 章节数、脚注闭环、渲染覆盖率、中文门全都会通过——
  省略号吞掉的正是这些检查看不见的内容维度。
