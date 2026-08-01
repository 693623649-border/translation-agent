---
name: pdf-translation-pipeline
description: 将影印版 PDF 编译为逐页 OCR、结构化目录、章节 Markdown、EPUB、带书签 PDF 和知识库 JSONL
version: 2.0.0
author: translation-agent contributors
---

# 影印书编译流水线

## 目标

主流程必须回答两个可审计问题：

1. PDF 每页包含什么文字？
2. 每个目录标题对应哪些 PDF 页面？

最终中间产物是 `chapters/`：每个 Markdown 文件对应一个章或节，并以该条目的标题作为一级标题。终端产物是文字版 EPUB/Word、可导入 RAG 的 JSONL，以及保留原书外观的带书签 PDF。

## 推荐入口

使用 `book_pipeline.py`，不要用旧的整本 `translation.md` 代替章节编译。

```bash
python book_pipeline.py input.pdf -o outputs/book --phase all --granularity chapter
```

入口只接受 PDF。收到图片 ZIP 时，必须先过滤 `__MACOSX` 等元数据文件，按自然页序排列，对每页应用 EXIF 方向并用 OSD 或人工抽样确认正文朝上，再按一图一页合成正向 PDF。不能把侧倒或颠倒图片直接交给 OCR，也不能只凭宽高判断日文竖排书页的方向。

## 阶段

### 1. OCR

Coding Plan 视觉 MCP 遇到 HTTP 1301 内容过滤时，不要对同一整页机械重试。框架会沿空白行把页面依次切为 4、8、16、32 个横带，仍调用同一 OCR 工具并按阅读顺序合并；空白横带的模型说明必须丢弃。若单行内容仍被确定性过滤，只允许对该页使用本地 OCR 并逐行对照原图，在页级 `ocr_model`/`notes` 中明确记录回退来源。

默认通过 Coding Plan 官方视觉 MCP 的 `extract_text_from_screenshot` 逐页识别，写入 `pages/page_XXXX.json` 和便于复核的 Markdown。文件是稳定检查点，重复运行默认跳过已有页。

```bash
python book_pipeline.py input.pdf -o outputs/book --phase ocr
```

允许通过 `--import-ocr-dir` 导入旧 `extracted_pages.json` 或 `_checkpoints/`。

视觉 MCP 不可用或需要本地后端时，允许使用 Tesseract，且必须安装相应语言包：

```bash
python book_pipeline.py input.pdf -o outputs/book --phase ocr \
  --ocr-backend tesseract \
  --tesseract-language jpn_vert+eng \
  --tesseract-psm 3
```

日文竖排使用 `jpn_vert+eng`，日文横排使用 `jpn+eng`。复杂竖排、多栏、注音页面必须抽样复核；局部失败时用 `--start-page` / `--end-page` 定位重跑。Tesseract 不需要 API Key，但后续自动目录解析仍需要 GLM/Coding Plan Key，非中文翻译则需要独立的 DeepSeek Key。

### 2. 目录与页码映射

把前置页 OCR 文本通过 Coding Plan 专属 OpenAI 兼容端点交给文本模型，生成 `toc.json`。已知目录页时优先传 `--toc-pages 6-10`；已有人工目录时传 `--toc-json FILE`。

程序在目录页后查找章、节、前言和结语标题，以 `PDF 页码 - 目录印刷页码` 统计主偏移，并用高置信度标题证据逐条写入实际 PDF 页。这样可处理漏扫空白页造成的中途偏移变化。缺少可靠证据且无法推断主偏移时必须停止，由用户传 `--page-offset N`，不能默默猜测；人工偏移始终优先。

### 3. 标题编译

```bash
python book_pipeline.py input.pdf -o outputs/book --phase compile --granularity chapter
```

文件名规则：`三位顺序号_目录序号_标题.md`。文件第一行必须是：

```markdown
# 目录序号 标题
```

按章合并使用互不重叠区间；按节或小节合并使用闭区间，换节页同时进入相邻两个文件，并在 manifest / 知识库元数据中标记 overlap。最后一节必须在下一章处结束，不能跨章吞并正文。

### 4. 发布

从结构化标题和章节 Markdown 生成：

- EPUB3 导航和 Word 标题结构；
- `knowledge_base.jsonl`，每条含章节标题和准确 PDF 页码；
- 原 PDF 的书签副本。

必须区分两层数据：

- `chapters/*.md`、`chapters.json`、`toc.json` 和知识库属于可审计层，保留来源 PDF 与页码映射；
- EPUB 和 Word 属于阅读发布层，必须通过 `strip_publication_metadata` 删除来源注释、PDF 页面锚点、页码注释和页尾印刷页码，不得按原 PDF 强制分页。

Word 可用 `--phase docx` 单独重建；`compile` 和 `all` 默认同时生成 EPUB 与 Word，可分别用 `--no-epub`、`--no-docx` 关闭。

## 非中文翻译扩展

原始 OCR 永远保存在 `PageRecord.text`。只有显式传入 `--translate-non-chinese` 时才翻译被检测为非中文的页，译文写入 `translated_text`。

日文 OCR 编译为简体中文的保守调用方式：

```bash
python book_pipeline.py input.pdf -o outputs/book --phase compile \
  --translate-non-chinese --target-language 简体中文 \
  --translation-provider deepseek --translation-model deepseek-v4-pro \
  --translation-api-timeout 120 --translation-max-chars 12000 \
  --translation-concurrency 16
```

翻译 Key、端点和模型应通过 `pipeline.toml` Profile 配置：Profile 保存 provider、adapter、base URL、model 和 `credential_env`，不保存原始 Key。默认翻译 Profile 使用 `deepseek-v4-pro`；模型切换用 `--translation-profile`，临时凭据切换用 `--translation-api-key-env`。`deepseek-chat` 与 `deepseek-reasoner` 已停止使用。翻译缓存必须同时校验 OCR SHA 与不含 Key 的 Profile 指纹；翻译提交必须采用页锁和 compare-and-swap，禁止陈旧译文覆盖新版 OCR。`--ocr-concurrency` 与 `--translation-concurrency` 分别控制独立 worker 池；共享重试和起始限速策略，遇到 429 时只降低受影响阶段。不要为了普通续传加入 `--force`。

DeepSeek V4 的思考模式默认是 `enabled`；翻译请求必须显式携带 `"thinking": {"type": "disabled"}`。翻译不需要长推理，关闭思考可降低延迟和 token 成本，并提高并行 worker 的有效吞吐。

目录发现与结构化依赖完整的候选目录文本，是 OCR 完成后的单个全局模型请求，不得为追求 worker 数而拆成互相冲突的目录片段。并行只用于语义上独立的逐页 OCR、翻译及其他可安全分片的模型任务。

翻译后端实现 `Translator` 协议。默认后端是 DeepSeek，OCR/目录仍为 GLM/Coding Plan；未来接入其他翻译 API 时不得改变逐页记录和章节编译格式。

## 安全约束

- Coding Plan API Key 只从 `GLM_CODING_API_KEY` / `Z_AI_API_KEY` 或本地 `.env` 注入。
- DeepSeek 翻译 Key 只从 `DEEPSEEK_API_KEY` 或进程环境注入；不得复用 GLM Key，也不得把 DeepSeek Key 发送到 GLM/Coding Plan 端点。
- Coding Plan 文本端点固定为 `https://open.bigmodel.cn/api/coding/paas/v4`；视觉 OCR 走官方 MCP，不把 Coding Plan Key 发到标准计费端点。
- GLM 目录结构化与 DeepSeek 翻译使用独立客户端、端点、超时、重试和限流状态；并行调度不得破坏逐页检查点。
- 不把 Key 写入源码、输出元数据、日志或提交记录。
- 自动匹配不确定时中止；不生成看似成功但页码错误的章节。
- 每页 OCR、目录 JSON、偏移证据和章节 manifest 都必须保留，以便追溯原 PDF。

## 旧入口

`pdf_text_agent.py` 仍可用于 OCR→翻译→总结→DOCX/PDF 的旧工作流。若目标是 EPUB 或知识库，先导入其逐页检查点，再由 `book_pipeline.py` 完成目录和章节编译。
