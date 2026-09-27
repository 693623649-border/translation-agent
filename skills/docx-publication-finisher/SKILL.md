---
name: docx-publication-finisher
description: 修复并验收已经生成的翻译 Word 书稿。当用户要求去除原文页码、删除长篇注释、修复 OCR 硬换行或异常字距、统一正文排版、整理真实脚注、清理输出中间物、批量重建 DOCX 或完成最终 Word 交付时使用；不用于从零开始 OCR 或翻译源书。
---

# Word 成品修复与交付

坚持“源级修复”：先修 reviewed Markdown、EPUB 导入语义、出版清洗、脚注映射或
Word publisher，再显式重建受影响的 DOCX。除非用户只要求只读诊断，否则不要把
派生 DOCX 当作事实源，也不要把一次手工编辑冒充成可重复流程。

开始修改前阅读 [验收门](references/acceptance-gates.md)。如果项目已有 canonical
publication report，沿用其身份和路径；只有 DOCX 而没有上游语义源时，先通过
`docx_semantic_migration.py` 建立可审计的 reviewed source，再从框架重建。

## 工作流

1. 从 manifest、publication report、配置或用户给出的路径确定本批次期望集合；把每个
   canonical DOCX 路径写入清单，不用 `*.docx` 或“最新文件”猜成品。
2. 在最上游修复页码识别、软换行、段落语义、样式、脚注或注释块；数字只有在
   上下文证明为来源页码时才删除，年份、公式、列表和表格数字必须保留。读者版按
   下述长注策略保留短注并删除超限脚注，不把长注继续挤在页面底部。
3. 用项目正式入口显式重建整批 Word。修框架代码时先加回归测试；不直接修补
   `word/document.xml` 来掩盖源数据问题。
4. 先过结构门：期望文件数完整、页码污染为零、普通正文硬换行为零、样式契约
   一致、真实脚注引用与定义闭环、无模型或工具污染。
5. 再过渲染门。优先使用 `publication_verifier.py` / `docx_render_gate.py`；Windows
   缺少 LibreOffice 时，可用 [隔离 Word 渲染器](scripts/render_docx_with_word.ps1)
   把显式路径导出为 PDF。渲染器必须用创建前后进程快照证明只有一个新 WINWORD PID。
6. 从渲染结果做风险分层视觉抽检，覆盖扉页/章首、普通正文、密集正文、脚注和
   译者注。任一残留页码、拉伸字距、错断行、脚注泄漏、裁切或伪空白页都回到
   上游修复并重建。
7. 只有结构、渲染和视觉门都通过后才交付；报告精确文件、动态统计、测试命令、
   renderer 和经人工判断的 warnings。

## 读者版长注策略

- 本项目的翻译 Word 默认按读者版处理：脚注定义达到 **150 个规范化字符（含正常
  空格）** 即视为长注。删除时必须同时删除正文引用和完整定义，短注继续保留并由
  Word 自动连续编号；不得只删脚注文本而留下孤立上标。
- EPUB 语义输出使用
  `python epub_semantic_import.py prune-long-footnotes -o <OUTPUT_DIR> --minimum-characters 150 --remove-standalone-page-markers`
  后再重建 DOCX。命令保留 `semantic/source_chapters` 原始证据，并更新 manifest、
  semantic audit 与 `audit/reader-edition-pruning.json`。
- EPUB 内联页码仅在导入阶段按来源元素的类名、非链接数字等结构证据移除；不得在 Markdown
  或 DOCX 中按“上标数字”外观批量删除，以免误伤正文引用和数学指数。
- 若用户明确要求学术版、校勘版或完整注释版，则以该要求覆盖读者版默认值；除此之外，
  不因脚注“真实”就把大段注释原样塞回成品。

## 批次安全

- 每个 publication identity 只登记一个 canonical DOCX；用户正在编辑或摘要冲突的
  文件必须保留并阻断替换。若为了避开占用而生成“已修复”副本，要在交付清单披露。
- 不把凭据、模型提示、checkpoint 名、临时图片路径、`python-docx` 默认作者或调试
  信息写入成品。
- 验收通过后，`outputs/` 只保留 manifest 明确列出的最终交付物。QA PDF/PNG、结构
  报告、日志、OCR smoke、章节缓存、语义中间层和源 EPUB 移入仓库内带时间戳的
  `.codex-trash/outputs-cleanup-*` 隔离区；先校验所有源路径确实位于 `outputs/`，不要
  用递归通配符删除，且在交付中说明可恢复位置。
- 框架代码与 skill 可提交 Git；大型书稿、渲染页图和输出报告默认继续留在忽略的
  `outputs/`，除非用户明确要求版本化这些产物。
