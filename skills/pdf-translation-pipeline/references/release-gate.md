# 发布质量门

## 两级检查

### 问题章节增量门

对 `--chapter-id` 指定的章执行快速确定性检查：

- manifest 条目、文件名、唯一 H1 和正文完整；
- `reviewed_chapters/<id>.md` 只在输入侧执行一次 BOM、外层空白和显式分页元数据清洗，结果必须与 `chapters/<filename>` 的成品字节精确一致；不得再次规范化成品来掩盖篡改；
- Markdown 脚注 `[^id]`/`[^id]:`、`〔n〕` 以及能被同章定义证明的 `[n]` 与尾注定义完整对应；采用引注体系的审定章同时阻断缺定义、孤儿定义和重复定义，纯注释清单的未引用定义作为 warning 人工确认；
- 不含 U+FFFD、内部 token、模型前言、来源 PDF 注释或分页锚点。

写入审定稿后，先用 `compile --no-verify --no-epub --no-docx --no-kb --no-bookmarked-pdf` 更新发布 Markdown，再用它验证单章修复；否则审定源稿与旧发布章必然不一致。增量报告单独写入 `audit/chapter-report.json`，不得覆盖代表整本状态的 `release-report.json`。增量门不解析全书容器。全部问题章通过后运行一次正常编译，由自动全书门完成最终验收。

### 全书发布门

全书门从当前 `chapters.json`、`toc.json` 与源 PDF 推导期望值，不硬编码任一本书的数量。

- 检查点：`pages/page_XXXX.json` 精确覆盖源 PDF 全页且 OCR 来源非空；要求翻译时，仅对实际进入非审定发布章的非中文页检查译文来源哈希、目标语言和可选 Profile 指纹。审定稿覆盖页保留原始 OCR 瑕疵只告警，不篡改审计层。
- Markdown：manifest 序号、ID、文件名和标题唯一，章节 `(id,title,pdf_page,level,kind)` 与 `toc.json` 在当前粒度下精确覆盖，结束页按 TOC 边界和源 PDF 正确派生；无多余旧章；所有审定覆盖精确往返。
- EPUB：ZIP 合法，`mimetype` 首项且不压缩；书名和语言元数据非空且与指定值/其他容器一致；manifest、spine、导航按章节一一对应；每章 reader-visible 全文和完整 H1–H6 `(level, title)` 签名与 Markdown 精确一致，缺失 body 直接失败。
- Word 内容：只验收图产物登记的单一 canonical DOCX，不用目录 glob 选择“最新”文件；唯一 Title/Codex Book Title 段、核心属性书名和 EPUB 书名一致；允许与核心作者一致的居中作者段；每章 reader-visible 全文与 Markdown 一致；Heading 1–3 签名精确相等（源 H4–H6 按生成规则折叠为 Heading 3）；表格形状、每条引文的原文/归属/顺序、粗体/斜体/下划线片段均精确对应。分页只允许扉页后的单一空分页段和一级章节标题的 `pageBreakBefore`，其他显式分页仍阻断；纯 `PAGE` 字段页脚可保留，任何额外页眉页脚文字仍阻断。
- Word 真实脚注包：正文脚注必须生成 `word/footnotes.xml` 中的真实 `w:footnote`，不得扁平化成正文、伪造普通段落或改用 endnotes。质量门检查 OPC content type、document relationship、引用和非保留定义 ID 的唯一性与双向闭环，并要求 `-1` separator、`0` continuation separator 两个保留节点存在且分别声明正确 `w:type`；正文不得引用保留 ID，任何孤儿引用、孤儿定义、重复 ID、非法关系、缺失 part 或意外 endnotes 都失败。报告使用稳定的 `docx_footnotes_*`、`docx_footnote_*` 和 `docx_unexpected_endnotes` issue code，并动态记录引用、定义、孤儿及保留节点计数。
- Word 引注语义：OCR 页脚/尾注的引用落点必须可追溯到注释定义和来源页。唯一且达到阈值的落点才可自动发布；低置信度、多候选或仅靠“最近句子”启发式得到的落点必须在 `audit/` 中列出章节、来源页、标记和候选位置，并作为 blocking check，不能降级为 warning。
- Word 渲染：包结构门通过后，在隔离、固定字体配置的 LibreOffice 或 Word 渲染环境打开 canonical DOCX 并生成 PDF/页图。检查渲染成功且页数非零、无异常空白或超密页、正文/脚注不越过页边界、表格不溢出、无非预期分页，并在报告记录 renderer、版本、字体配置和页数。渲染器或所需字体不可用时该检查必须 `skipped`/失败，使报告成为 `partial` 且非 release-ready；不能以 OOXML 结构检查代替视觉渲染门。
- 知识库：每行合法 JSON；字段及其类型严格符合 `id`、`title`、`chapter_id`、`chapter_order`、`content` schema；提供源 PDF 时重算 SHA-1 稳定 ID；块顺序和章节顺序正确；按章拼接的完整内容与 Markdown 正文一致且无孤儿章。
- 参考 PDF：必须提供源 PDF，页数与源 PDF 相等；书签逐项等于 `toc.json` 经封面/封底过滤和层级归一后的标题、层级与目标页；逐页页面尺寸/旋转、可复制文字层和低分辨率 RGB 渲染哈希均与源 PDF 相同。书签数不要求等于编译章节数。
- 发布卫生：Markdown、EPUB、Word、知识库无来源分页、模型说明、乱码或内部占位符；OCR、校勘、翻译的 stage lock 均未被占用；无编译临时文件或 `_page_images_<pid>_<uuid>` 崩溃遗留目录。

### Word 专用发布门

Word Recipe 使用独立的 `publication.word_report`，而不是把仅含 DOCX 的任务伪装成完整多格式报告。它仍执行检查点、manifest、审定稿往返、语义脚注、引注闭环、发布卫生、DOCX OOXML 结构、真实脚注包、固定字体 LibreOffice 渲染和运行时卫生检查。EPUB、知识库和参考 PDF 明确不在该发布范围内；除此之外的任何 skipped 检查都会使报告不可发布。直接以 `publication.docx` 为目标只能得到中间产物。

## 报告与失败处理

完整多格式报告位于 `OUTPUT/audit/release-report.json`；Word 专用报告位于 `OUTPUT/audit/word-release-report.json`。检查项使用稳定 ID，数量位于 `metrics`/`summary`，每个失败项给出对应 artifact 与原因。完整发布必须同时满足 `publication_profile=full`、`mode=full`、`ok=true`、`release_ready=true`、`status=passed` 和 `summary.skipped=0`；主动跳过任一容器的 full 检查返回 `partial` 且非 release-ready。Word 发布必须满足 `publication_profile=word`、`mode=full`、`ok=true`、`release_ready=true`、`status=passed`，且 skipped 集合只能包含 EPUB、知识库和参考 PDF 三项。

1. 先查看失败 check，不凭日志猜测。
2. 修正最上游的 reviewed Markdown、TOC 或逐页检查点。
3. 单章问题先轻量编译再重跑增量门；容器、知识库或书签问题重新正常编译，由自动全书门验收。
4. 不删除或手工编辑已生成的 EPUB/DOCX/JSONL 来掩盖上游错误。
5. 报告失败时不得称任务完成；warning 需要人工判断并在交付说明中披露。

框架代码发生变化时，发布门之外再跑全量工程测试、依赖检查、语法检查和补丁检查。书籍内容变化只运行增量门与最终全书门，以减少重复工作。
