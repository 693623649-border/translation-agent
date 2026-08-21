# Word 成品验收门

这些门禁固化已验证的 Word 收尾流程。每次运行都必须从当前 manifest 或显式清单
动态取得文档数、页数、脚注数和抽检数；不得复用另一批书的历史数字。

## 源级清理门

重建前在语义源或 publisher 中处理：

- 只删除有强证据的来源页码：独立数字页标、明确分页元数据相邻的连续数字，或 EPUB
  中非链接页码 superscript/独立页码块。不要全局删除短数字。
- 保留链接 noteref、真实脚注、年份、公式、列表、章节号、表格数据和引文数字；
  引用与定义缺失、重复或孤立都阻断发布。
- 合并 OCR/复制产生的视觉软换行；保留作者明确的段落、标题、列表、表格、诗歌短行
  和显式 `<br>`。
- 译者注/来源注形成独立语义块，不并入前一正文段；正文恢复后不得继续吞入注释样式。
- 清除 `[空白页]`、重复 EPUB cover/titlepage 脚手架、模型前言、调试路径、页 JSON 名、
  临时渲染名和 `python-docx` 默认作者污染。

对于 EPUB，本项目已经在 `epub_semantic_import.py` 区分非链接页码 superscript 与真实
链接注释；对于 OCR/Markdown，本项目只在显式分页证据下移除连续小数字。改变这些
规则时必须同时增加“页码被删除”和“合法数字被保留”的对照回归测试。

## 重建门

The Word files must be regenerated through the agent framework, not patched manually:

- `actual_docx_count == expected_docx_count`；两者来自当前清单，不写死为九本或任何历史值。
- 每个路径来自 manifest、report 或明确配置；命令可从仓库脚本和配置重放。
- canonical 文件若被用户修改/占用，不静默覆盖；保留冲突文件并显式产出安全副本或阻断。
- 修框架时至少运行针对性测试；交付前运行全量工程测试和静态检查。

本仓库的 canonical 输出优先通过 `book_pipeline.py ... --phase compile` 自动运行完整门，
或用 Word Recipe 以 `publication.word_report` 为目标。`publication.docx` 只是中间产物。

## 结构门

Inspect the DOCX package and generated reports before rendering:

- 数字独立正文段、`[空白页]`、非分页普通 `w:br`、`python-docx` 污染、重复封面/扉页
  和旧字体 token 均为零。
- Normal 正文为 SimSun、两端对齐、字符间距 0；标题为项目标题样式及 Microsoft YaHei；
  译者注/来源注为独立缩进且左对齐，不污染恢复后的正文。
- 有注释时必须存在合法 `word/footnotes.xml`，正文使用 superscript Footnote Reference；
  引用与正 ID 定义唯一双向闭环，保留的 `-1`/`0` separator 节点类型正确，无意外 endnotes。
- 每个显式文件都有 passed 结果；任何 DOCX 基础检查 skipped 都不能声称完成。

## 渲染门

Render every canonical DOCX and inspect machine-readable render results:

- 每个 canonical DOCX 都成功渲染，报告记录 renderer、版本/环境、逐文档页数和动态总页数。
- 伪空白页、裁切、溢出、页边界外文字、缺字字体替换和失败导出均为零。
- 渲染器只接受显式 DOCX 路径。Windows fallback 可以与用户已有 Word 共存，但必须
  比较 COM 激活前后的 WINWORD 进程集合，证明恰有一个新 PID；只关闭该实例。
- OOXML 结构检查不能替代渲染；渲染器不可用时结果是 partial/failed，不得发布。

## 视觉抽检门

Sample rendered pages before delivery:

- 每份文档至少覆盖开篇/章首、普通或密集正文以及该文档的最高风险页面；有脚注、译者注、
  表格或诗歌时追加对应样张。类似的同版式书批可从每本三张开始，但要按风险增加，不能
  把“三张”当作所有文档的充分证明。
- 检查页码残留、脚注上标与定义位置、软换行、字距、两端对齐、缩进、裁切、溢出、
  伪空白页和标题孤页。

Any visual sample showing residual source page numbers, footnotes inside the body, stretched spacing, broken paragraphs, inconsistent alignment, clipping, or blank output fails the gate.

## 交付清单

The final handoff must include:

- 精确列出每个 DOCX 一次，不用模糊目录链接。
- 报告结构审计、renderer、逐文档/总页数、视觉样张位置和动态样本数。
- 报告针对性测试、全量测试、语法/依赖/补丁检查，以及 warnings 的人工判断。
- 大型输出默认不提交 Git；只提交可重复的框架、skill、脚本、测试和文档。

## 已验证基准（仅作校准证据）

促成本 skill 的一次真实运行覆盖 9/9 份 Word、1533 个渲染页和 27 张风险样张：来源
页码被移除，真实脚注保留为上标，硬换行/字距/正文样式和译者注得到修复，结构、几何
和视觉门均通过。这些数字用于证明流程曾在真实批次上工作，未来运行必须重新计算。
