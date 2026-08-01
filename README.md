# 影印书编译 Agent

这个仓库的主流程是：

```text
影印版 PDF
  → 每页 OCR（回答“每页有什么文字”）
  → 目录识别与结构化 JSON（回答“每个标题从哪一页开始”）
  → 书内页码 / PDF 页码偏移校准
  → 按标题生成章节 Markdown 文件夹
  → 文字版 EPUB/Word + AI 知识库 JSONL + 带书签的参考 PDF
```

主入口是 `book_pipeline.py`。旧的 `pdf_text_agent.py`（逐页 OCR、翻译、总结、DOCX/PDF）仅为兼容已有 `_checkpoints` 保留，不再是影印书编译的推荐入口。根目录的 `patch_translations.py` 和 `extract_textbook_layer.py` 是新格式检查点的人工辅助工具；一次性书籍脚本和旧监控器已移到 `archive/`，详情见 `archive/README.md`。

## 仓库结构

```text
translation-agent/
├── book_pipeline.py              # 当前主入口
├── translation_agent_api.py      # 程序化调用接口
├── frontend_app.py               # Streamlit 低代码控制台
├── frontend_service.py           # 安全子进程、日志和产物服务层
├── launch_frontend.py            # 一键启动前端
├── pipeline_profiles.py          # Provider/Profile 配置与模型指纹
├── pipeline_runtime.py           # 共享重试和起始限速器
├── pipeline.example.toml         # 不含密钥的模型配置示例
├── pdf_text_agent.py             # 兼容旧检查点的旧入口
├── patch_translations.py         # 新格式译文人工修补工具
├── extract_textbook_layer.py     # 新格式文本层提取工具
├── archive/
│   ├── karatani/                 # 硬编码单本书的一次性脚本
│   └── legacy/monitor.py         # 仅适用旧 Windows 流程
├── tests/
├── requirements.txt
└── .env.example
```

## 低代码 Web 控制台

安装依赖后，只需一条命令：

```bash
python launch_frontend.py
```

浏览器会打开 `http://127.0.0.1:8501`。界面按“选择任务 → 填写凭据与
输出 → 开始/继续任务”组织，可直接完成：

- 上传 PDF 或填写服务器 PDF 路径；
- 下拉切换 OCR、目录和翻译 Profile；
- 密码框临时注入各 Profile 对应的 API Key；
- 选择一键全流程或单独的 OCR、目录、翻译、Markdown、EPUB、Word 阶段；
- 调整 OCR/翻译 worker、页范围、目录页、页码偏移和日文竖排模式；
- 查看实时日志、断点状态并下载 EPUB、Word、知识库或带书签 PDF。

在远程服务器运行：

```bash
python launch_frontend.py --host 0.0.0.0 --port 8501 --no-browser
```

如服务器没有额外访问控制，建议通过 SSH 端口转发访问，不要把该端口直接
暴露到公网。前端密码框中的 Key 只进入任务子进程的环境变量；不会拼进命令
参数或保存到 Profile。子进程日志在显示前还会再次执行密钥脱敏。

## 为什么原框架不够

旧流程只生成整本 `extracted_text.md` / `translation.md`，缺少以下关键数据和产物：

- 目录页自动发现及可人工覆盖的目录页范围；
- 固定 schema 的目录 JSON；
- 书内印刷页码到 PDF 页码的偏移量；
- 每章或每节一个 Markdown 文件；
- EPUB、RAG/知识库 JSONL、PDF 书签。

`book_pipeline.py` 补齐了这些环节，并让每一步都可单独运行、检查和重跑。

## 安装与配置

```bash
pip install -r requirements.txt
cp .env.example .env
```

OCR 和目录结构化默认使用 Coding Plan；非中文翻译使用独立的 DeepSeek API。在本地 `.env` 中分别填写两套凭据：

```bash
GLM_API_MODE=coding-plan
GLM_CODING_API_KEY=your-coding-plan-key
GLM_CODING_API_BASE=https://open.bigmodel.cn/api/coding/paas/v4
GLM_TEXT_MODEL=glm-5.2
OCR_BACKEND=coding-plan-mcp
CODING_PLAN_VISION_MCP_COMMAND=npx -y @z_ai/mcp-server@latest
OCR_CONCURRENCY=4

TRANSLATION_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-deepseek-api-key
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_API_TIMEOUT=120
TRANSLATION_CONCURRENCY=16
```

推荐把非敏感的端点、模型和 worker 数放进
`pipeline.example.toml` 这样的 Profile 文件；Profile 只保存
`credential_env = "DEEPSEEK_API_KEY"`，绝不保存 Key 本身：

```bash
cp pipeline.example.toml pipeline.toml
export GLM_CODING_API_KEY='...'
export DEEPSEEK_API_KEY='...'

python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase all --config pipeline.toml
```

切换模型只需选择另一个 Profile：

```bash
python book_pipeline.py -o "outputs/my_book" \
  --phase translate --config pipeline.toml \
  --translation-profile deepseek_flash \
  --translate-non-chinese --translation-source-language ja
```

轮换 Key 时只更新 `credential_env` 指向的环境变量。原始 Key 不应写入
TOML、命令行、逐页 JSON 或日志。旧的 `--api-key`、
`--ocr-api-key`、`--translation-api-key` 仅为兼容保留，已经从帮助信息
隐藏，使用时会输出安全警告。

Coding Plan 的 OpenAI 兼容文本端点负责目录 JSON，不直接接收图片。OCR 通过套餐官方的视觉理解 MCP Server 调用 `extract_text_from_screenshot`；翻译则通过独立 DeepSeek 客户端完成，不会把日文 OCR 文本或 DeepSeek Key 发给 GLM。因此需先安装 Node.js 18 或更新版本，并确保 `npx` 可用：

```bash
node --version
npx -y @z_ai/mcp-server@latest
```

密钥不会写入输出文件。OCR/目录 Key 与翻译 Key 必须分别使用 `GLM_CODING_API_KEY` 和 `DEEPSEEK_API_KEY`；推荐只用环境变量，避免密钥出现在 shell 历史或进程列表。

DeepSeek 默认模型是 `deepseek-v4-pro`，OpenAI 兼容地址保持为 `https://api.deepseek.com`。`deepseek-v4-flash` 仍可作为显式的低成本覆盖值，但不再是本流水线默认值。官方已公告旧模型名 `deepseek-chat` 和 `deepseek-reasoner` 已于北京时间 2026-07-24 23:59 停止使用，因此新配置不要再使用这两个兼容别名；详见 [DeepSeek V4 更新日志](https://api-docs.deepseek.com/zh-cn/updates) 与 [模型说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。

DeepSeek V4 的思考模式默认启用。本流水线的翻译是确定性的文本转换任务，因此每个翻译请求都显式发送 `"thinking": {"type": "disabled"}`，减少额外推理延迟和 token 消耗，并提高多 worker 并发吞吐；参见 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)。

若另有按量计费的标准 GLM API Key，也可改用专用 `glm-ocr` 接口：

```bash
OCR_BACKEND=glm-ocr
GLM_OCR_API_KEY=your-standard-api-key
```

标准 `glm-ocr` 不计入 Coding Plan；两种 Key 和端点不要混用。

### 本地 Tesseract OCR（可选后端）

视觉 MCP 暂时受限或希望完全在本机完成 OCR 时，可使用 Tesseract。程序会继续生成相同的逐页 JSON/Markdown 检查点，后续目录、翻译和编译流程不变。Debian/Ubuntu 上处理日语书页至少需要：

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-jpn tesseract-ocr-jpn-vert tesseract-ocr-eng
tesseract --list-langs
```

日文竖排页推荐 `jpn_vert+eng`，横排页改用 `jpn+eng`；`--tesseract-psm 3` 让 Tesseract 自动判断版面：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase ocr \
  --ocr-backend tesseract \
  --tesseract-language jpn_vert+eng \
  --tesseract-psm 3
```

Tesseract OCR 本身不需要 API Key，但自动目录解析仍需要 GLM/Coding Plan Key，把非中文 OCR 翻译为中文则需要独立的 DeepSeek Key。复杂竖排、注音或多栏页面建议抽样复核；可以只对失败页结合 `--start-page`、`--end-page` 和 `--force` 重跑。

### ZIP 图片输入先转为正向 PDF

`book_pipeline.py` 的输入是 PDF，不直接读取 ZIP。图片 ZIP 必须先解压、按自然页序排序，忽略 `__MACOSX`、`.DS_Store` 等元数据文件，对每张图应用 EXIF 方向并确认正文实际朝上，再按“一张图片对应一页”合成 PDF。不要仅凭文件宽高猜方向；日文竖排书页也应保持整页正向，文字栏通常从右向左排列。建议抽查首、中、末页，确认没有 90°/180° 倒置后再运行 OCR：

```text
book-images.zip
  → 解压并过滤元数据文件
  → 自然排序（1, 2, …, 10，而不是 1, 10, 2）
  → 应用 EXIF/OSD 或人工校正旋转
  → 合成 book_正向.pdf
  → 交给 book_pipeline.py
```

方向错误会同时降低视觉模型和 Tesseract 的识别质量；先修正源页，比翻译阶段补救可靠。

## 一条命令完成

```bash
python book_pipeline.py "input.pdf" \
  -o "outputs/my_book" \
  --phase all \
  --granularity chapter \
  --translate-non-chinese \
  --translation-provider deepseek \
  --ocr-concurrency 4 \
  --translation-concurrency 16
```

程序默认：

1. 把 PDF 每页渲染为临时 JPG，通过 Coding Plan 视觉 MCP 调用 GLM-4.6V OCR，按 `--ocr-concurrency` 并行保存逐页 JSON；若整页触发 1301 内容过滤，则自动沿空白行切成 4/8/16/32 个有序横带，仍使用同一 MCP 补识别并按句法衔接；
2. 将前 40 页 OCR 文本交给 GLM 判断目录页并输出目录 JSON；
3. 在目录之后寻找章节标题，计算 `PDF 页码 - 书内页码`；
4. 按章生成带审计页码的 Markdown；
5. 如开启翻译，以独立 DeepSeek API 按 `--translation-concurrency` 并行翻译非中文页，再通过发布清洗层生成不含原始分页和来源页码的 EPUB/Word，同时生成知识库 JSONL 和带书签 PDF。

逐页 OCR 和逐页翻译都是可分片的模型阶段，分别使用独立 worker 池并尽可能并行；目录发现与结构化以整本书的候选目录文本为一个整体请求，必须等 OCR 检查点齐备后执行，不做会破坏上下文一致性的切片并发。两个 worker 数都可以按各自服务的速率限制独立调整。

逐页 OCR 结果会自动断点续传；稳定输出目录可重复使用。临时页图默认删除，使用 `--keep-page-images` 可保留以便复核。分段补救会丢弃空白横带返回的“无可见文字”说明，并以 `mcp-segmented-*` 请求标记保留审计线索；如果单行本身仍被服务拒绝，应只对该页使用本地 OCR 并对照原图复核，不能留下缺页或把过滤说明混入正文。

## 推荐的可审计分步流程

### 第一步：逐页 OCR

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr
```

OCR 结果位于 `outputs/my_book/pages/page_XXXX.json`，并同步生成便于人工复制的 `page_XXXX.md`。每页记录包含 PDF 页码、Markdown 文本、语言检测、可选译文和 OCR 模型。

也可以导入旧框架结果，无需重新 OCR：

```bash
python book_pipeline.py "input.pdf" \
  -o "outputs/my_book" \
  --phase ocr --skip-ocr \
  --import-ocr-dir "outputs/old_run"
```

导入器兼容 `extracted_pages.json`、`_checkpoints/page_XXXX.json` 和新格式的 `pages/page_XXXX.json`。

### 第二步：目录 JSON 与偏移量

全自动：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase toc
```

如果肉眼已经确认目录在 PDF 第 6–10 页，建议显式指定，结果更稳定：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase toc --toc-pages 6-10
```

如果目录 JSON 已手工整理，可完全跳过目录 LLM：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase toc --toc-json manual_toc.json --page-offset 12
```

手工 JSON schema：

```json
{
  "toc_pdf_pages": [6, 7],
  "entries": [
    {
      "index": "第一章",
      "title": "复调小说与陀思妥耶夫斯基创作",
      "level": 1,
      "kind": "chapter",
      "printed_page": 1
    },
    {
      "index": "一",
      "title": "问题的提出",
      "level": 2,
      "kind": "section",
      "printed_page": 3
    }
  ]
}
```

`printed_page` 是目录中印出的书内页码；程序计算并写回 `pdf_page`。程序先统计全书的主偏移，再以 OCR 中高置信度的章、节、前言和结语标题页逐项覆盖映射，因此也能处理漏扫空白页导致的中途偏移变化。缺少可靠标题证据且无法推断主偏移时会停止并要求传入 `--page-offset`，不会静默猜测；显式传入的偏移量始终优先。

### 第三、四步：章节 Markdown、EPUB、Word 和知识库

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase compile --granularity chapter \
  --require-complete-ocr --required-ocr-model-prefix coding-plan/
```

按节合并：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase compile --granularity section
```

按章时使用不重叠页区间；按节/小节时使用闭区间，换节页同时进入前后两节，且最后一节会在下一章处结束，不会越界。小节粒度使用 `--granularity subsection`。`chapters.json` 会记录 `boundary_mode`，知识库记录会标记 `boundary_overlap`，便于后续人工或模型清理边界。

所有章节文件都以标题编译：

```markdown
# 第一章 复调小说与陀思妥耶夫斯基创作

<!-- source-pdf: input.pdf -->
<!-- pdf-pages: 19-56 -->
```

EPUB 导航、Word 标题和 PDF 书签均从这些结构化标题生成，而不是从正文猜测。

章节 Markdown 是可审计中间层，保留 `source-pdf`、`PDF_PAGE` 和页码范围。EPUB 与 Word 是阅读发布层，生成时会统一删除这些来源注释、原 PDF 分页锚点和页尾印刷页码，也不会按原 PDF 强制分页。可单独重新生成 Word：

```bash
python book_pipeline.py -o "outputs/my_book" --phase docx --title "书名"
```

## 非中文 OCR 的 DeepSeek 翻译接口

程序总会保留原始 `text`。检测到日文、英文、韩文、俄文等非中文文本时，可显式开启翻译。独立的 `translate` 阶段不依赖 `toc.json`，因此可以在 OCR 仍在写入逐页检查点时分批运行：

```bash
python book_pipeline.py -o "outputs/my_book" \
  --phase translate \
  --config pipeline.toml --translation-profile deepseek_pro \
  --translate-non-chinese \
  --target-language 简体中文 \
  --translation-api-timeout 120 \
  --translation-max-chars 12000 \
  --translation-concurrency 16
```

翻译写入每页 JSON 的 `translated_text`，并记录对应 OCR 文本的
SHA-256、翻译服务商、模型、目标语言、提示词版本和 Profile 指纹。
指纹包含 provider、adapter、base URL、model、目标语言和提示词版本，
但不包含 API Key。OCR 文本或任一指纹字段变化后，旧译文都会在翻译
阶段进入重译队列；编译阶段也会按当前 Profile 严格核对，不能把旧
Flash 译文当成 Pro 译文。翻译保存采用页级文件锁和 OCR SHA
compare-and-swap；若模型返回前 OCR 已更新，陈旧译文会被丢弃而不会
覆盖新版 OCR。

临时切换凭据时使用 `--translation-api-key-env ENV_NAME`；切换地址和
模型优先新建 Profile，也可使用 `--translation-api-base` 和
`--translation-model`。`--translation-api-timeout` 覆盖 Profile
超时；`--translation-max-chars` 控制每个翻译分块的最大字符数。整本
原文语言明确时可用 `--translation-source-language ja` 覆盖逐页自动
判断；`--start-page` / `--end-page` 约束独立翻译阶段。

完整的日语图片书工作流可以分阶段执行，便于复核和断点续传：

```bash
# 先对已经转正的 PDF 做本地日语 OCR
python book_pipeline.py "book_正向.pdf" -o "outputs/book" \
  --phase ocr --ocr-backend coding-plan-mcp \
  --ocr-reading-direction vertical \
  --ocr-cache-model-prefix coding-plan/

# OCR 过程中可反复调用 DeepSeek，只翻译新出现的日文页
python book_pipeline.py -o "outputs/book" \
  --phase translate --translate-non-chinese --target-language 简体中文 \
  --config pipeline.toml --translation-profile deepseek_pro \
  --translation-source-language ja \
  --translation-api-timeout 120 --translation-max-chars 12000 \
  --translation-concurrency 16

# 确认/生成 toc.json 后，编译标题级产物
python book_pipeline.py "book_正向.pdf" -o "outputs/book" \
  --phase compile \
  --granularity chapter --title "中文书名" \
  --require-complete-ocr --required-ocr-model-prefix coding-plan/ \
  --require-translation
```

`TextChatBackend.model_identity()` 是 Provider 的强制接口，新增后端必须
明确声明身份，不能依赖 `getattr` 猜模型名。再次执行相同 Profile
会跳过新鲜译文；切换 Profile 会按指纹自动重译。

查看状态不需要源 PDF：

```bash
python book_pipeline.py -o "outputs/book" --phase status \
  --config pipeline.toml
```

输出包含逐页数量、OCR 模型分布、源文本新鲜译文数、当前 Profile
新鲜译文数以及现有发布产物。

## Python 调用接口

```python
from translation_agent_api import RunRequest, run_book

result = run_book(
    RunRequest(
        input_pdf="book/input.pdf",
        output_dir="outputs/input",
        phase="all",
        config="pipeline.toml",
        translation_profile="deepseek_pro",
        translate_non_chinese=True,
        source_language="ja",
        require_complete_ocr=True,
        require_translation=True,
    )
)
assert result.ok, result.status
```

`RunRequest` 不提供 API Key 字段。调用进程通过 Profile 中的
`credential_env` 解析凭据，因此可以安全地轮换账号或切换模型。

## 输出结构

```text
outputs/my_book/
├── pages/
│   ├── page_0001.json
│   ├── page_0001.md           # 便于直接复制和人工复核的原始 OCR
│   └── ...
├── toc.json                  # 目录、偏移证据、PDF 页码映射
├── chapters/
│   ├── 001_第一章_....md
│   └── ...
├── chapters.json             # 章节文件清单与页区间
├── knowledge_base.jsonl      # 带章节和 PDF 页码元数据的 RAG 记录
├── 书名.epub                  # 无原 PDF 分页信息的 EPUB3
├── 书名.docx                  # 无原 PDF 分页信息的 Word 文档
└── 书名_带目录.pdf            # 原版外观 + 可复制层（若原有）+ 书签
```

## 常用参数

```text
--phase all|ocr|translate|toc|compile|epub|docx|status
--config pipeline.toml       Provider/Profile 配置
--ocr-profile NAME           覆盖 OCR Profile
--toc-profile NAME           覆盖目录文本 Profile
--translation-profile NAME   覆盖翻译 Profile
--toc-pages 6-10,12          人工指定 PDF 目录页
--toc-json FILE              使用人工目录 JSON
--page-offset N              人工指定 PDF 页码减书内页码
--api-mode coding-plan|standard
--ocr-backend coding-plan-mcp|glm-ocr|tesseract
--ocr-concurrency 4          逐页 OCR worker 数
--ocr-reading-direction horizontal|vertical  内容过滤分段的阅读方向；日文竖排用 vertical
--ocr-cache-model-prefix coding-plan/  仅复用指定模型来源，自动覆盖本地兜底页
--tesseract-language jpn_vert+eng  Tesseract 语言包；横排日文用 jpn+eng
--tesseract-psm 3             Tesseract 页面分割模式
--granularity chapter|section|subsection|all
--translate-non-chinese      仅翻译检测为非中文的页
--translation-source-language auto|ja|en  覆盖自动语言检测
--translation-provider deepseek   翻译后端（默认 deepseek）
--translation-api-key-env NAME  从指定环境变量读取翻译 Key
--translation-api-base URL   DeepSeek OpenAI 兼容端点
--translation-model NAME     翻译模型（默认 deepseek-v4-pro）
--translation-api-timeout 120  翻译 API 单次请求超时
--translation-max-chars 12000  单个翻译分块的最大字符数
--translation-concurrency 16  逐页翻译 worker 数
--start-page / --end-page    局部 OCR、翻译或联调
--force                      重做已有 OCR/翻译检查点
--require-complete-ocr       编译前要求逐页 OCR 覆盖整个源 PDF
--required-ocr-model-prefix coding-plan/  编译前拒绝本地兜底 OCR
--require-translation        编译前拒绝缺失或已过期译文
--no-epub / --no-docx / --no-kb / --no-bookmarked-pdf
```

## 旧流程

仅在需要复用旧 `_checkpoints` 时运行旧的翻译、总结和 DOCX/PDF 管线：

```bash
python pdf_text_agent.py "input.pdf" -o outputs \
  --llm-core deepseek --ocr-llm-core mimo --keep-page-images
```

旧入口与主入口都优先读取 `DEEPSEEK_API_BASE`；为兼容旧 `.env`，`pdf_text_agent.py` 仍接受 `DEEPSEEK_BASE_URL`。若两者同时存在，以 `DEEPSEEK_API_BASE` 为准。旧的 Windows 监控器和三份绑定单本书的脚本位于 `archive/`，不属于可维护的调用接口。

旧流程详情保留在源码参数帮助中：

```bash
python pdf_text_agent.py --help
```
