---
name: pdf-translation-pipeline
description: 将影印版或文字层 PDF 转换为可审计的逐页文本、目录映射、标题级中文 Markdown、EPUB、Word、知识库 JSONL 和带书签 PDF。用户要求 OCR、翻译、断点续跑、重生成问题章节、切换模型配置、编译或发布前完整验收时使用。
---

# PDF 翻译编译与验收

## 使用主入口

在仓库根目录运行 `book_pipeline.py`。输入必须是方向正确的 PDF；图片 ZIP 先过滤系统元数据文件、应用 EXIF/人工旋转、按自然页序合成 PDF。

```bash
python book_pipeline.py SOURCE.pdf -o OUTPUT --phase all \
  --config pipeline.toml --granularity chapter
```

把 `pages/*.json`、`toc.json`、`chapters.json` 和 `audit/` 视为可追溯审计层。把 `chapters/*.md`、EPUB、Word 和知识库 JSONL 视为阅读发布层；发布层不得保留来源 PDF 字段、PDF/书内页码、分页锚点、内部物理页边界或模型处理痕迹。带书签 PDF 是保留原书外观的参考件，不适用去分页规则。

## 按阶段执行

1. 运行 `--phase ocr`，将每个 PDF 页保存为独立稳定检查点。默认并行 4 个 Coding Plan GLM-4.6V worker；续跑时复用匹配当前模型指纹的完成页，不要无故使用 `--force`。
2. 运行 `--phase toc`，从完整候选目录文本生成 `toc.json` 并校准书内页到 PDF 页的映射。目录是一个全局结构请求，不要为了并行而拆坏上下文；证据不足时要求显式 `--toc-pages`、`--printed-pages-per-pdf-page` 或 `--page-offset`。
3. 仅在明确要求时运行 `--phase proofread` 校勘 OCR；非中文内容运行 `--phase translate --translate-non-chinese`。OCR、校勘和翻译可按页并行，分别保留检查点与模型指纹。
4. 运行 `--phase compile --require-complete-ocr`，按结构化标题生成章节 Markdown、EPUB、Word、知识库和带书签 PDF；翻译任务同时加 `--require-translation`。未显式指定 `--granularity` 时保持已有清单粒度，避免续跑时意外改变章节数量；编译结束会自动执行发布质量门。

只发布 Word 时使用内置 Recipe，并仍把发布报告作为最终目标：

```bash
python graph_pipeline.py SOURCE.pdf -o OUTPUT --phase all \
  --config pipeline.toml --recipe recipes/chinese-pdf-word.toml
```

PDF 自带可靠 outline 时改用 `recipes/outline-word.toml`。这两个 Recipe 只关闭知识库、EPUB 和参考 PDF publisher，不关闭验证节点，目标必须是 `publication.word_report`。Word gate 仍要求语义审计、真实脚注 OOXML 结构门及固定字体 LibreOffice 渲染门；`publication.docx` 只是未验收的中间产物。不要用 `--target publication.docx`、`--no-verify`、`--no-docx-render` 或 API 的 `verify_publication=false` 覆盖该发布契约。

## 处理 Word 返工问题

用户反馈已经生成的 Word 存在来源页码、错误换行、异常字距/对齐、脚注或版面
问题时，切换到 `$docx-publication-finisher`。它负责源级修复、显式批量重建、
结构门、隔离渲染、视觉抽检和精确交付清单；本 skill 继续负责 PDF/EPUB 到
`publication.word_report` 的主编译流程。不要直接编辑 canonical DOCX。

OCR 继续使用 GLM/Coding Plan Profile；中文翻译默认使用独立 DeepSeek `deepseek-v4-flash` Profile，并显式关闭思考模式。模型、端点、worker 和 `credential_env` 写入 `pipeline.toml`，原始 Key 只从环境变量注入；不要写入源码、argv、输出或日志。用 `--ocr-concurrency`、`--proofread-concurrency`、`--translation-concurrency` 独立调整 worker。

简体转换必须调用框架的 `normalize_target_script()` 词法保护，不要用全局“著→着”替换：`望著→望着` 可以转换，但作者义和词汇义的 `所著`、`名著`、`显著` 必须保留。框架改动后保留这组三类回归样例。

## 重生成问题章节

当某章存在阅读障碍、错序、断句或注释问题时：

1. 回到源 PDF 对应连续页面和逐页 OCR，按标题、段落、引文、图注、表格、正文引注及尾注的逻辑结构重建整章；不要继续拼接已损坏的逐页译文。
2. 将审定结果原子写入 `OUTPUT/reviewed_chapters/CHAPTER_ID.md`。唯一一级标题必须精确等于 `chapters.json` 中的 `display_title`。
3. 先执行一次只更新 Markdown 的本地编译，使审定源稿进入发布章；跳过容器生成和自动全书门：

```bash
python book_pipeline.py SOURCE.pdf -o OUTPUT --phase compile --no-verify \
  --no-epub --no-docx --no-kb --no-bookmarked-pdf
```

4. 再运行增量章节门，只检查改动章：

```bash
python book_pipeline.py SOURCE.pdf -o OUTPUT --phase verify \
  --chapter-id CHAPTER_ID \
  --report OUTPUT/audit/chapter-report.json
```

5. 所有问题章通过后只运行一次正常 `compile`；它会生成所有格式并自动执行全书门，无需紧接着重复调用 `verify`。沿用原任务的 `--config`、`--title`、`--target-language` 和生成格式选项，并加 `--require-complete-ocr`；翻译任务再加 `--require-translation`。只有验收既有产物或单独重建某个格式后，才显式运行全书 `--phase verify`。不要直接修补 EPUB、Word 或知识库。

章节门从当前正文动态提取引注与注释定义：支持 `〔n〕`、Markdown `[^id]`，以及与本章尾注定义相互印证的 `[n]`；普通 `[2022]` 年份不误判。采用正文引注体系的审定章必须双向闭环，缺定义、正文删除后遗留的定义及重复定义都会失败；只包含注释清单而正文未引用的特殊章节给出 warning，必须人工确认。同时检查审定源稿经一次发布清洗后与发布 Markdown 的规范字节精确一致、唯一 H1、乱码、内部占位符、模型前言及来源分页痕迹。英文书目、专名和原文引文可以合法存在，不要仅凭拉丁字母比例自动删文。

OCR 页脚或尾注进入正文语义层时，必须保存“正文引用落点—注释定义—来源页”关系及落点置信度。只有唯一且达到阈值的落点才能自动发布；多候选、仅靠距离猜测或低于阈值的映射必须写入 `audit/` 并阻断最终报告，不能静默把编号附到最近句子。人工复核后应修改上游审定 Markdown/语义记录，再重新生成 Word。

## 执行发布质量门

在宣告任务完成前，必须阅读并遵守 [references/release-gate.md](references/release-gate.md)。独立验收命令不调用模型：

```bash
python book_pipeline.py SOURCE.pdf -o OUTPUT --phase verify \
  --require-translation \
  --report OUTPUT/audit/release-report.json
```

上例为翻译任务；中文原书省略 `--require-translation`。整本均为人工审定稿时添加 `--require-all-reviewed`。只有命令退出码为 0，报告同时满足 `mode=full`、`ok=true`、`release_ready=true`、`status=passed`、`summary.skipped=0`，且 warnings 已人工判断并披露，才能宣告发布完成。按报告中的动态数字说明章节、引注、知识库块、PDF 页和书签结果，不复用其他书或历史运行的计数。

Word Recipe 的正式报告为 `OUTPUT/audit/word-release-report.json`，还必须满足 `publication_profile=word`、`ok=true`、`release_ready=true`、`status=passed`；允许且只允许 EPUB、知识库和参考 PDF 三项因不在 Word 发布范围内而 skipped。`docx.structure`、`docx.render`、`semantics.integrity` 或任何基础检查不得 skipped。

完整书签/PDF、逐页 OCR 覆盖和知识库稳定 ID 验收必须传入真正的源 PDF。省略源文件并加 `--no-bookmarked-pdf` 只能得到 `partial` 报告，不得声明完整发布。

质量门验证：源 PDF 全页 OCR 检查点与应译范围的新鲜译文；`toc.json` 到 manifest 的章节、标题、层级、类型及页范围完整覆盖；审定稿往返一致；EPUB 书名/语言元数据、manifest/spine/nav、逐章全文与完整 H1–H6 签名；Word 书名与首章前置区、逐章全文、完整标题签名、表格、引文原文、粗体/斜体/下划线、真实脚注 OOXML 包及固定环境渲染结果；知识库字段白名单、稳定 ID、顺序、逐章全文与全章覆盖；书签 PDF 页数、书签标题/层级/目标页，以及逐页几何、可复制文字层和低分辨率 RGB 外观均与源 PDF 相同；所有阅读格式无来源分页或模型痕迹；OCR、校勘、翻译阶段锁均已释放，且无临时文件或崩溃遗留图片目录。

Word publisher 只登记并交付图产物指向的单一 canonical DOCX；不要在输出目录用 `*.docx` glob 猜测成品，也不要并存手工修订副本。旧的框架托管成品只能由 publication identity 按已记录摘要安全替换；用户修改过的冲突文件必须保留并由质量门阻断，等待人工处理。

## 提高执行效率

- 逐页 OCR、校勘、翻译和相互独立的问题章节可并行；共享目录结构化与最终编译保持单次执行。
- 内容修订期间用轻量本地编译配合 `--chapter-id` 增量门；它只用于 `--phase verify`，值可为 manifest `id` 或十进制 `sequence`。全部章节就绪后只跑一次正常编译及其自动完整发布门。
- 纯书籍内容变化不重复运行整套工程测试。仅在框架代码或 skill 改动时运行 `python -m unittest discover -s tests -q`、`python -m compileall -q .`、`python -m pip check` 和 `git diff --check`。
- 失败时读取报告中 `status != passed` 的 check，不重复 OCR 或模型阶段；修正最上游的 Markdown/目录/检查点后重建下游产物。

## 兼容入口

`pdf_text_agent.py` 仅保留旧流程兼容。目标包含标题级 Markdown、EPUB 或知识库时，导入旧逐页检查点后仍由 `book_pipeline.py` 完成编译与验收。
