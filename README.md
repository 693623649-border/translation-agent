# 影印书编译 Agent

这是一个 local-first 的文档语义翻译与出版工具。扫描 PDF、带文字层 PDF 和
EPUB 通过显式 Source Adapter 进入同一语义、清洗与发布契约；DAG 负责依赖闭包、
断点缓存和产物身份；已接入对应 verification profile 的文件必须通过发布门后才
可以正式交付。

## 五分钟开始

需要 Python 3.11+（推荐 3.12）：

```bash
python3.12 -m pip install -e ".[web]"
translation-agent-doctor --web
cp pipeline.example.toml pipeline.toml
```

先预览 DAG，不调用模型：

```bash
translation-agent plan book/input.pdf -o outputs/input \
  --source-mode text-pdf --config pipeline.toml
```

本地启动任务工作台：

```bash
translation-agent-web
```

浏览器访问 `http://127.0.0.1:8501`。WebUI 只允许 loopback 监听；远程使用时，
请保持服务绑定 `127.0.0.1`，再通过带认证的 SSH 隧道访问。

扫描 PDF 的成熟主流程是：

```text
影印版 PDF
  → 每页 OCR（回答“每页有什么文字”）
  → 目录识别与结构化 JSON（回答“每个标题从哪一页开始”）
  → 书内页码 / PDF 页码偏移校准
  → 按标题生成章节 Markdown 文件夹
  → 文字版 EPUB/Word + AI 知识库 JSONL + 带书签的参考 PDF
```

Graph 还提供 `--source-mode text-pdf`，用于每页都有完整可复制
文字层的 born-digital PDF。该模式执行
`source.inspect → pages.text_extract → pages.translate → toc/compile → semantic`
并继续进入相同的 publisher/verifier；它不会注册或调用 OCR 节点。默认仍是
`scanned-pdf`，系统不会根据 PDF 内容自动猜测入口。

产品入口是 `translation-agent`（`document_pipeline.py`）；现有 PDF 全链仍由
`graph_pipeline.py` 执行，它把各阶段组织成可替换的依赖图，同时
复用 `book_pipeline.py` 的成熟实现和全部旧参数。`book_pipeline.py` 仍是完全
兼容的阶段式入口。旧的 `pdf_text_agent.py`（逐页 OCR、翻译、总结、DOCX/PDF）
仅为兼容已有 `_checkpoints` 保留，不再是影印书编译的推荐入口。根目录的
`patch_translations.py` 和 `extract_textbook_layer.py` 是新格式检查点的人工
辅助工具；一次性书籍脚本和旧监控器已移到 `archive/`，详情见
`archive/README.md`。

影印 PDF、带文本层 PDF 和 EPUB 的统一语义层顺序、CLI 迁移名和发布阻断标准
见 [`docs/unified-semantic-dag.md`](docs/unified-semantic-dag.md)。EPUB 当前可执行
命令另见 [`docs/epub-semantic-input.md`](docs/epub-semantic-input.md)。EPUB-native
正式发布门尚未接入，因此 CLI/WebUI 会明确将这条路径标为草稿，不能伪造
`release_ready`。产品层、版本化契约和 WebUI 安全边界见
[`docs/product-architecture.md`](docs/product-architecture.md)。

## 本版本改动（2026-08）

### 发布清洗层（book_pipeline.py + publication_verifier.py）

针对输出侧三类常见问题：

1. **分段混乱（跨页断词/断句）**：`should_join_page_boundary` 按字元边界
   拼接页末"作为对"+下一页"象"为"作为对象"；译者/编者/原注结尾行拒绝
   与下页正文拼接，页末脚注不再粘到下一页。
2. **注释位置混乱**：脚注编号保留门改为整页级判定——表格/年表行
   （`|`、`〇`）、编号 `0`、无标点标题行、跨行续行标记等 OCR 噪声不再
   误报"漏译脚注"；翻译提示词显式禁止输出日文原文，并新增 kana 占比门禁
   （译文假名 >20% 判失败），DeepSeek 只校勘不翻译的页面会被阻断。
3. **原图页数错误出现在译文**：独立 2–3 位边页码、跨行拆分的印刷页码
   （竖排 OCR 把"40"拆成 `4`/`0` 两行）循环清除、运行页眉+页码
   （"导论 17"）按标题子片段匹配剥离；验证器期望文本镜像同一规则，
   保证 EPUB/Word 与章节 Markdown 精确一致。

### 章节注释重组工具（tools/note_reflow.py）

针对从 Word/LaTeX 导出的论文型 PDF（正文段与页脚注定义段交错导入，
注释定义插在正文段落之间），提供上游重组：

- 注释定义统一移入章末 `## 注释`（编号全书连续）；
- 正文行内裸数字引用（"。3 "、"，4 "、"迪士尼15，"）标记化为 `〔n〕`；
- 缺失引用按定义行前最近正文段回填，保证引用 ↔ 定义双向闭环；
- 错位 URL 续行按内容关键词归位，定义行内混入的正文引述
  （"第xvii页。拉马尔写道：…"）切回正文流，正文残句段与承接段拼接。

### 两级验收门

- **增量门**（`--phase verify --chapter-id`）：审定稿单次清洗后精确往返、
  正文引注↔尾注定义双向闭环、唯一 H1、无乱码/占位符/模型前言/分页痕迹；
- **全书门**（compile 自动执行）：11 项无模型检查，覆盖检查点、manifest、
  EPUB/Word 结构、知识库稳定 ID、PDF 书签与页面外观、运行卫生。

### 测试

完整单元测试套件覆盖 Graph 规划与缓存、发布清洗、页码处理、注释门禁、
验收门与重组工具。测试数量会随功能演进，因此以命令实际输出为准：

```bash
python -m unittest discover tests
```

### 项目级 Agent 工作流

仓库在 `skills/` 中提供可自动发现的 Codex skill。`pdf-translation-pipeline`
负责从 PDF/EPUB 语义输入到发布产物的主流程；`docx-publication-finisher`
负责已经生成的 Word 成品出现来源页码、OCR 硬换行、异常字距、正文对齐、
脚注或版面问题时的源级修复、显式批量重建、结构审计、隔离渲染、视觉抽检
与最终交付清单。后者不直接手改派生 DOCX，也不以少量截图代替整批结构门。

框架或 skill 发生变化后，除相关功能测试外还应运行：

```bash
python -m unittest tests.test_project_skills
python -m unittest discover -s tests -q
python -m compileall -q .
git diff --check
```

## 仓库结构

```text
translation-agent/
├── document_pipeline.py          # 版本化 RunSpec 的统一产品 CLI
├── graph_pipeline.py             # 可组合 DAG 命令行入口
├── book_pipeline.py              # 兼容的阶段式执行入口与成熟节点实现
├── pipeline_graph/
│   ├── core.py                   # NodeSpec、拓扑规划、缓存、事件与执行器
│   ├── book.py                   # book_pipeline 的 Graph 适配节点
│   └── recipe.py                 # 严格 Recipe 与 allowlist 插件注册表
├── recipes/
│   ├── full-publication.toml     # 完整出版物
│   ├── chinese-pdf-word.toml     # 中文 PDF → Word
│   └── outline-word.toml         # PDF 内置目录 → Word
├── product_contracts.py          # RunSpec / ArtifactRecord / Event 公共契约
├── semantic_ir.py                # 版本化文档语义 IR 与人工复核决定
├── semantic_apply.py             # 翻译集合验证与事务回填
├── translation_agent_api.py      # 传统与 Graph 程序化调用接口
├── application_service.py        # CLI/WebUI 共用任务应用服务
├── frontend_runtime.py           # SQLite WAL、后台任务、安全环境与产物目录
├── streamlit_app.py              # Streamlit 多页产品入口
├── app_pages/                    # 新任务、状态、产物、设置页面
├── frontend_app.py               # 旧入口兼容 shim
├── launch_frontend.py            # local-only 一键启动前端
├── doctor.py                     # 运行环境预检
├── repository_guard.py           # 密钥、大文件和本地书稿防泄漏门
├── pipeline_profiles.py          # Provider/Profile 配置与模型指纹
├── pipeline_runtime.py           # 共享重试和起始限速器
├── publication_verifier.py       # 无模型调用的统一发布质量门
├── pipeline.example.toml         # 不含密钥的模型配置示例
├── pdf_text_agent.py             # 兼容旧检查点的旧入口
├── patch_translations.py         # 新格式译文人工修补工具
├── extract_textbook_layer.py     # 新格式文本层提取工具
├── tools/note_reflow.py          # 章节注释重组工具（见下文）
├── skills/                       # Codex 项目级工作流与发布验收指令
├── archive/
│   ├── karatani/                 # 硬编码单本书的一次性脚本
│   └── legacy/monitor.py         # 仅适用旧 Windows 流程
├── tests/
├── pyproject.toml
├── requirements.txt
└── .env.example
```

## Graph 执行引擎

`graph_pipeline.py` 是 `book_pipeline.py` 之上的轻量依赖图，不是另一套不兼容
的流水线。内置节点仍调用原有 OCR、校勘、翻译、目录、编译及发布阶段，因此
继续使用相同的 `pages/page_XXXX.json`、`toc.json`、章节目录、模型 Profile、
页级 CAS 和断点检查点。Graph 只接管节点选择、依赖规划、节点缓存、事件记录
和输出目录互斥。原有命令可以继续使用；要获得可组合能力时，把入口改为
`graph_pipeline.py`，其余 `book_pipeline.py` 参数保持不变。

### 先查看计划

`--plan` 只解析配置并输出节点的 `requires`、`provides`、版本、资源锁和缓存
设置，不调用模型，也不生成出版物：

```bash
python graph_pipeline.py "book/input.pdf" -o "outputs/input" \
  --phase all --config pipeline.toml \
  --recipe recipes/full-publication.toml --plan
```

执行时会根据目标自动选择依赖闭包，而不是依赖源码中的固定调用顺序。可用
`--target publication.docx` 临时把目标收窄到 Word，但该目标只是未验收的
中间产物；正式 Word 交付必须以 `publication.word_report` 为目标。

### 内置 Recipe

Recipe 只描述“启用哪些节点、禁用哪些节点、需要哪些最终产物”。常用调用为：

```bash
# 完整版本：章节 Markdown、知识库、EPUB、Word、参考 PDF 和发布验收
python graph_pipeline.py "book/input.pdf" -o "outputs/input" \
  --phase all --config pipeline.toml \
  --recipe recipes/full-publication.toml

# 已是中文的 PDF：OCR、目录、章节 Markdown，只发布 Word
python graph_pipeline.py "book/中文书.pdf" -o "outputs/中文书" \
  --phase all --config pipeline.toml \
  --recipe recipes/chinese-pdf-word.toml

# 使用 PDF 内置 outline 代替目录 LLM，只发布 Word
python graph_pipeline.py "book/有书签的书.pdf" -o "outputs/有书签的书" \
  --phase all --config pipeline.toml \
  --recipe recipes/outline-word.toml
```

`outline-word` 要求源 PDF 确实包含可用书签；没有 outline 时节点会明确失败，
不会静默退回模型目录。Recipe 是严格、纯数据 TOML，支持的字段只有
`schema_version`、`id`、`targets`、`enable`、`disable` 和
`required_plugins`。两份 Word Recipe 的最终产物是
`publication.word_report`；完整多格式 Recipe 的最终产物才是
`publication.report`。

### 节点与产物

内置节点按实际 phase 和 Recipe 按需加入：

| 节点 | 职责 / 主要产物 |
| --- | --- |
| `core.source.inspect` | 校验并指纹化源 PDF（`source.pdf`） |
| `core.pages.import` | 一次性导入旧格式逐页检查点 |
| `core.pages.load` | 读取已有逐页检查点（`pages.raw`） |
| `core.pages.ocr` | PDF 逐页 OCR（`pages.raw`） |
| `core.pages.text_extract` | 显式读取完整 PDF 文字层（`pages.raw`），不调用 OCR |
| `core.pages.proofread` | 非破坏性 OCR 校勘覆盖层（`pages.proofread`） |
| `core.pages.translate` | 非中文页面翻译（`pages.translated`） |
| `core.toc.load` | 读取已有映射目录（`toc.mapped`） |
| `core.toc.resolve` | 人工目录或目录 LLM 解析及页码映射 |
| `core.toc.from_outline` | 从 PDF 内置 outline 生成映射目录 |
| `core.chapters.load` | 读取已有章节 Markdown |
| `core.chapters.compile` | 页面和目录编译为章节 Markdown |
| `core.reconstruct.semantic` | 建立正文块与脚注引用—定义关系；不确定落点阻断发布 |
| `core.publication.sanitize` | 幂等清除页眉、页码及内部出版标记 |
| `core.publish.knowledge_base` | 发布知识库 JSONL |
| `core.publish.epub` | 发布 EPUB |
| `core.publish.docx` | 发布 Word |
| `core.publish.reference_pdf` | 发布带书签的参考 PDF |
| `core.publication.verify.word` | 验收真脚注 DOCX 的 OOXML 结构与固定字体渲染，产出 `publication.word_report` |
| `core.publication.verify` | 验收完整多格式出版物，产出 `publication.report` |
| `core.pipeline.status` | 读取检查点与产物状态 |

每个 `NodeSpec` 明确声明 `requires`、`provides`、版本、缓存指纹和资源锁。
规划器会在执行前阻断缺依赖、重复 Provider 和循环依赖。每次执行在输出目录
写入：

```text
outputs/my_book/.pipeline_graph/
├── source.json                    # 输出目录绑定的源 PDF 路径与 SHA-256
├── ocr_identity.json              # OCR 内容语义身份，不含 Key/worker
├── proofread_identity.json        # 校勘模型、提示词、分块等内容语义
├── translation_identity.json      # 翻译模型、思考模式、分块等内容语义
├── import_identity.json           # import 管理页与安全清理摘要
├── publication_identity.json      # 框架管理的成品路径/摘要，用于安全换标题
├── chapter_drafts/                # compile → sanitize 的不可变输入 bundle
│   ├── chapters.json              # 与该快照配套的 manifest
│   └── *.md                       # 尚未发布清洗的章节草稿
├── state.json                     # 节点指纹、产物摘要和可恢复状态（原子替换）
├── events.jsonl                   # run/node started、skipped、succeeded、failed
└── output.lock                    # 持久诊断文件；内核 advisory lock 表示实际占用
```

artifact 中声明的 `path` 是权威数据位置，不能假定它一定等于输出目录中的
传统文件名。需要调用旧阶段的 consumer 会先校验摘要，再把输入 artifact
原子物化到兼容位置：页面 bundle 物化为 `pages/`，目录 artifact 物化为
`toc.json`。发布器则直接读取 `chapters.reader` 的 `chapter_dir` 和 `manifest`，
不会绕回读取可能陈旧的 canonical `chapters/`。最后由 verifier 把 reader
bundle 发布到 `chapters/` / `chapters.json`，并把内置或替代 publisher 的
文件 artifact 发布到 Word、EPUB、知识库和参考 PDF 的 canonical 文件名后再
验收。这样插件可以在独立暂存目录生产结果，同时旧验证器仍只检查唯一的正式
发布候选。替代 publisher 不得把非 canonical 文件直接放在输出根目录，否则
会形成第二个同格式候选并被 Graph 明确拒绝；应写入 `.pipeline_graph/` 下的
独立暂存目录，或直接提供 canonical 文件名。

Graph 缓存不替代原有页级检查点。OCR 节点每次都会进入旧阶段，让 PageStore
逐页核对自动推导出的**精确** `ocr_model` 身份；该身份包含后端、模型、阅读
方向和提示词版本。显式 `--ocr-cache-model-prefix` 是导入旧检查点时保留的宽松
兼容逃生口，会关闭 Graph 自动注入的 exact match，日常运行不应使用它放宽
模型校验。OCR 的 DPI、端点或 MCP command 改变，以及校勘/翻译的模型、思考
模式或 `max_chars` 改变时，对应 stage identity sidecar 会让旧阶段收到
`--force`；worker、delay、timeout 和 Key 轮换不改变内容身份，也不会无谓重做。
stage identity 按 PDF 页保存；局部执行只更新所选页，随后全书执行仍能发现并
重做其他语义陈旧页。`--import-ocr-dir` 保持加法导入，不会删除无关检查点；
导入源后来删页时，也只清除仍与上次导入摘要一致的旧管理页，人工或模型阶段
改过的页会被保留并退出 import 管理范围。
`--force-node` / `--force-graph` 控制 Graph 节点缓存，旧参数 `--force` 则显式
要求旧阶段重做其页级结果。

首次处理一个全新输出目录时，Graph 会把源 PDF 的 SHA-256 写入
`.pipeline_graph/source.json`；以后若把另一份 PDF 指向同一输出目录会在 OCR
前直接失败，防止按相同页号误用旧缓存。对升级前已经完整生成、但没有源绑定
的旧输出，确认 PDF 无误后可仅在首次运行增加 `--adopt-existing-output`；该操作
要求检查点恰好覆盖源 PDF 的 `1..N` 全部页面。无法确认来源时应使用新目录。

`book_pipeline.py` 的公共 `main()` 与 Graph 共用同一个
`.pipeline_graph/output.lock`，所以两个入口不能同时改写同一输出目录。Graph
持锁期间只通过内部 unlocked seam 调用旧阶段，以避免嵌套自锁；外部代码仍应
调用公共 CLI/API，不能直接调用这个内部 seam。`output.lock` 文件在释放后仍会
保留，是否占用由内核 advisory lock 判定，而不是按文件是否存在或旧 PID 猜测；
进程崩溃时内核会自动释放，因此不存在并发删除“陈旧锁”的竞态。

Word、EPUB 与参考 PDF 的文件名随有效书名变化。Graph 在
`publication_identity.json` 中记录自己生成的路径和 SHA-256；换标题时只删除
仍与记录摘要一致的旧成品。若用户后来编辑过旧文件，Graph 会保留它并让发布门
报告重复候选，不会替用户删除内容。

### Python 中规划、替换和删除节点

低代码 API 使用 `GraphRunRequest` 包装原有 `RunRequest`：

```python
from pipeline_graph import NodeResult, NodeSpec
from translation_agent_api import GraphRunRequest, RunRequest, prepare_graph

request = GraphRunRequest(
    pipeline=RunRequest(
        input_pdf="book/中文书.pdf",
        output_dir="outputs/中文书",
        phase="all",
        config="pipeline.toml",
        generate_epub=False,
        generate_knowledge_base=False,
        generate_bookmarked_pdf=False,
        verify_publication=True,
    ),
    recipe="recipes/chinese-pdf-word.toml",
)
prepared = prepare_graph(request)       # 只构图，不执行
print([node.name for node in prepared.plan()])

# 用自己的清洗器替换同名节点；requires/provides 契约保持不变。
def my_sanitize_handler(context):
    drafts = context["chapters.markdown"]
    # 该插件辅助函数必须把清洗结果写入独立目录，例如
    # .pipeline_graph/my_reader/，并在该目录写自己的 chapters.json。
    # 不要就地修改 drafts，也不要直接改 canonical chapters/。
    chapters = build_reader_bundle(
        drafts,
        destination=context.output_dir / ".pipeline_graph" / "my_reader",
    )
    return NodeResult(
        outputs={"chapters.reader": chapters},
        fingerprints={"chapters.reader": chapters["sha256"]},
    )

old = next(
    node for node in prepared.graph.nodes
    if node.name == "core.publication.sanitize"
)
custom = NodeSpec(
    name=old.name,
    handler=my_sanitize_handler,         # (GraphContext) -> NodeResult
    requires=old.requires,
    provides=old.provides,
    version="my-sanitizer-v1",
    fingerprint="my-sanitizer-config-v1",
    resources=old.resources,
    cache=False,                         # 未提供文件校验器时不要复用节点缓存
)
prepared.graph.replace(old.name, custom)

# remove 会真正移除节点；若目标仍依赖它，plan() 会在执行前报告缺依赖。
removed = prepared.graph.remove("core.publication.sanitize")
prepared.graph.add(custom)              # 可换成另一个提供 chapters.reader 的节点

result = prepared.execute()
```

上例中的 `build_reader_bundle` 代表插件自己的实现：返回值至少应包含独立的
`chapter_dir`、`manifest`、`sha256` 和 `count`。这个独立 bundle 是下游
publisher 的直接输入；只有 verifier 才负责把它发布到 canonical 章节目录，
因此替换 sanitize 不会污染 compile 的不可变 `chapter_drafts`。

节点 Handler 必须返回 `NodeResult(outputs={...})`，且键必须与 `provides`
完全一致。要删除 EPUB、知识库等发布模块，优先在 `RunRequest` 中关闭对应
`generate_*`，或在 Recipe 的 `disable` 中删除节点并同步调整 `targets`；不要
在依赖它的目标保持启用时强行删除。

`pipeline.argv` 是私有控制输入：Key 与吞吐参数不会进入它的指纹，配置文件则
只记录内容 SHA-256。外部节点若读取自定义文件或环境语义，仍必须提供自己的
callable `fingerprint`（只纳入所依赖的非敏感内容），否则将该节点设为
`cache=False`；不要让默认缓存猜测节点未声明的外部状态。Prepared Graph 创建后
若同一路径的 Profile 内容发生变化，执行会要求重新 `prepare`，避免一次运行中
不同节点读到不同配置版本。

### Profile、Recipe 与插件安全边界

Profile 和 Recipe 必须严格分离：

- `pipeline.toml` / `--config` 是 **Profile**，只负责 Provider、模型、端点、
  worker、阅读方向和 `credential_env`；原始 Key 仍只放环境变量。
- `recipes/*.toml` / `--recipe` 是 **Recipe**，只负责目标和拓扑选择；禁止
  API Key、端点、环境变量名、Python import/callable 或 shell command。

外部节点由已安装 Python 包的
`translation_agent.graph_nodes` entry-point group 提供。Recipe 只能声明：

```toml
required_plugins = ["acme_cleanup"]
enable = ["acme.cleanup_headers"]
```

调用者还必须单独授权，Recipe 本身不能给插件执行权限：

```bash
python graph_pipeline.py "book/input.pdf" -o "outputs/input" \
  --phase all --config pipeline.toml --recipe recipes/acme.toml \
  --allow-plugin acme_cleanup
```

allowlist 只是显式授权，不是沙箱或代码签名。加载 entry point 会在当前进程
执行该包的 Python 代码，因此只能安装并 allowlist 自己审查和信任的插件。
外部插件默认禁用、不能注册或覆盖 `core.*`，并且必须同时出现在 Recipe 的
`required_plugins` 和调用者的 `--allow-plugin` 中。插件也不能提供保留的私有
控制值 `pipeline.argv`；其安装 distribution、版本和 entry-point 会进入节点
身份，升级或换包后不会继承另一实现的旧缓存。

要用插件替换中间组件，不需要让它冒充 `core.*`：让插件节点提供相同产物，
例如 `chapters.reader`，并在 Recipe 中设置
`disable = ["core.publication.sanitize"]`、
`enable = ["acme.cleanup_headers"]`。规划器会在执行前确认新的 Provider 能
完整接上所有下游；缺口或重复 Provider 都会直接报错。

### `all` 与可选校勘

为了保持旧行为和避免对整本书产生额外模型调用，传统入口及 Graph 的
`--phase all` **默认都不包含** `core.pages.proofread`。需要校勘时任选一种：

```bash
# 单次命令启用
python graph_pipeline.py "book/input.pdf" -o "outputs/input" \
  --phase all --config pipeline.toml --include-proofread

# 或在自定义 Recipe 中启用
# enable = ["core.pages.proofread"]
```

Python 调用则设置 `GraphRunRequest(include_proofread=True)`。校勘节点会插在
OCR 与翻译/目录/编译之间，仍使用 Profile 选择的 `proofread_profile`；仅在
Profile 中填写 `proofread_profile` 不会自动启用该节点。

## 低代码 Web 控制台

WebUI 是 `ApplicationService` 的薄客户端：每个任务使用独立 UUID 工作区，任务
状态和事件保存在 SQLite WAL；浏览器关闭后后台任务继续运行，失败或取消后可从
Graph 检查点恢复。密钥只进入本次子进程的 allowlist 环境，不写入 RunSpec、
SQLite、日志或命令行。

安装 Web extra 后启动：

```bash
python3.12 -m pip install -e ".[web]"
translation-agent-web
```

浏览器会打开 `http://127.0.0.1:8501`。顶部导航包含“新任务、任务状态、产物、
设置”四页，可直接完成：

- 上传扫描 PDF、文字 PDF、EPUB，或选择 allowlist 根目录内的服务器文件；
- 下拉切换 OCR、目录和翻译 Profile；
- 密码框临时注入各 Profile 对应的 API Key；
- 先预览真实 DAG，再创建后台任务；
- 查看结构化状态、最近日志、取消/恢复任务；
- 只下载与 Graph 产物身份和通过的 release report 哈希一致的正式产物。

EPUB 原生发布验证尚未接入，界面会固定关闭该入口的“正式发布质量门”，产物只
标记为草稿。PDF 任务只有在 `release_ready=true` 且报告与 Graph 身份匹配时才
显示“正式产物”。

启动器拒绝 `0.0.0.0` 和其他非 loopback 地址。远程服务器请保持本机绑定：

```bash
translation-agent-web --host 127.0.0.1 --port 8501 --no-browser
# 在客户端另建带认证的 SSH 隧道：localhost:8501 → server:127.0.0.1:8501
```

在加入认证、租户隔离和反向代理安全策略之前，不支持直接公网暴露。

## 为什么原框架不够

旧流程只生成整本 `extracted_text.md` / `translation.md`，缺少以下关键数据和产物：

- 目录页自动发现及可人工覆盖的目录页范围；
- 固定 schema 的目录 JSON；
- 书内印刷页码到 PDF 页码的偏移量；
- 每章或每节一个 Markdown 文件；
- EPUB、RAG/知识库 JSONL、PDF 书签。

`book_pipeline.py` 补齐了这些环节，并让每一步都可单独运行、检查和重跑。

## 安装与配置

Graph 与 Profile/Recipe TOML 需要 Python 3.11+；推荐 Python 3.12。macOS
自带的 Python 3.9 不能直接运行 `graph_pipeline.py`。

```bash
python3.12 -m pip install -e .          # 核心 CLI
python3.12 -m pip install -e ".[web]"  # 加 WebUI
# 兼容旧脚本时使用：python3.12 -m pip install -e ".[legacy]"
cp .env.example .env
```

OCR 和目录结构化默认使用 Coding Plan；非中文翻译使用独立的 DeepSeek API。在本地 `.env` 中分别填写两套凭据：

```bash
GLM_API_MODE=coding-plan
GLM_CODING_API_KEY=your-coding-plan-key
GLM_TOC_API_KEY=your-coding-plan-key
GLM_CODING_API_BASE=https://open.bigmodel.cn/api/coding/paas/v4
GLM_TEXT_MODEL=glm-5.2
OCR_BACKEND=coding-plan-mcp
CODING_PLAN_VISION_MCP_COMMAND=npx -y @z_ai/mcp-server@0.1.4
CODING_PLAN_SPREAD_SEGMENTS=2
CODING_PLAN_VERTICAL_PAGE_ROWS=2
CODING_PLAN_VERTICAL_PAGE_COLUMNS=3
CODING_PLAN_VISION_REQUEST_DELAY=15
CODING_PLAN_VISION_MIN_REQUEST_DELAY=5
CODING_PLAN_VISION_MAX_REQUEST_DELAY=60
CODING_PLAN_VISION_SPEEDUP_WINDOW=8
CODING_PLAN_SEGMENT_ATTEMPTS=4
CODING_PLAN_SEGMENT_RATE_LIMIT_DELAY=15
CODING_PLAN_SEGMENT_RATE_LIMIT_MAX_DELAY=60
OCR_CONCURRENCY=4
Z_AI_MODE=ZHIPU
Z_AI_VISION_MODEL_MAX_TOKENS=4096

TRANSLATION_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-deepseek-api-key
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_TIMEOUT=120
TRANSLATION_CONCURRENCY=16
```

推荐把非敏感的端点、模型和 worker 数放进
`pipeline.example.toml` 这样的 Profile 文件；Profile 只保存
`credential_env = "DEEPSEEK_API_KEY"`，绝不保存 Key 本身。
`credential_env` 必须是合法的环境变量名；若误填原始 Key，配置加载会立即拒绝：

日文竖排书可直接在 OCR Profile 中设置
`reading_direction = "vertical"`；命令行参数仍可临时覆盖。OCR 执行、缓存
复用、编译前检查与状态页会共同使用这一设置，避免同一批页面被误判为
其他阅读方向的旧缓存。

```bash
cp pipeline.example.toml pipeline.toml
export GLM_CODING_API_KEY='...'
export GLM_TOC_API_KEY='...'
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
npx -y @z_ai/mcp-server@0.1.4
```

仓库、示例 Profile 和诊断提示统一固定为 `@z_ai/mcp-server@0.1.4`；不要改成
`@latest`，否则上游包更新会在没有配置变更的情况下改变 OCR 行为和缓存身份。

密钥不会写入输出文件。OCR/目录 Key 与翻译 Key 必须分别使用 `GLM_CODING_API_KEY` 和 `DEEPSEEK_API_KEY`；推荐只用环境变量，避免密钥出现在 shell 历史或进程列表。

DeepSeek 默认模型是 `deepseek-v4-flash`，OpenAI 兼容地址保持为 `https://api.deepseek.com`。需要 Pro 时可显式选择 `deepseek_pro` Profile，但它不再是本流水线默认值。官方已公告旧模型名 `deepseek-chat` 和 `deepseek-reasoner` 已于北京时间 2026-07-24 23:59 停止使用，因此新配置不要再使用这两个兼容别名；详见 [DeepSeek V4 更新日志](https://api-docs.deepseek.com/zh-cn/updates) 与 [模型说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。

## 非破坏性 OCR 校勘层

视觉 OCR 的原始 `text` 始终保留不动。遇到竖排错序、重复行、明显错字或
断行时，可在指定页上运行独立校勘阶段：

```bash
python book_pipeline.py -o "outputs/my_book" \
  --phase proofread --config pipeline.toml \
  --proofread-profile deepseek_flash \
  --proofread-language ja \
  --start-page 34 --end-page 37 \
  --proofread-concurrency 8 \
  --proofread-delay 0 --proofread-max-chars 12000
```

校勘提示词只要求修复日文 OCR，明确禁止翻译、概述和补写。结果作为
`proofread_text` 覆盖层写入逐页 JSON，并同时记录原始 OCR SHA-256、
语言、服务商、模型、提示词版本和 Profile 指纹。`pages/page_XXXX.md`
仍是原始 OCR，便于逐字审计。原始 OCR 一旦变化，旧覆盖层会自动失效，
读取和编译立即回退原始 `text`。
横向双页扫描会在 JSON 中额外保存按书内页码递增排列的
`physical_page_texts`（日文书为右页、左页）；校勘和翻译分别写入
`proofread_physical_page_texts` 与 `translated_physical_page_texts`。模型按物理页
独立处理，因此不需要把特殊分隔符发给模型，也不会在发布文本中泄漏内部边界。

后续翻译、目录解析和章节编译统一读取 `effective_text`（新鲜校勘层优先，
否则原始 OCR）。译文 SHA-256 也绑定 `effective_text`：校勘提交后，基于
旧 OCR 的译文会自动进入重译队列；并发返回的旧译文会被页级 CAS 拒绝，
不会覆盖新校勘。旧逐页 JSON 无需迁移；没有校勘字段时行为与此前完全一致。

`[pipeline]` 可设置 `proofread_profile = "deepseek_flash"`。省略时自动复用
`translation_profile`，因此通常不需要新增 Key。Python/低代码调用可在
`RunRequest` 中设置 `phase="proofread"`、`proofread_profile`、
`proofread_language`、`proofread_concurrency`、`proofread_delay` 和
`proofread_max_chars`。状态输出提供 `proofread_pages_source_fresh`、
`proofread_pages_profile_fresh` 与 `proofread_models`。

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

### 本地 PaddleOCR（GPU Docker 后端，auto 默认优先）

本机部署的 PaddleOCR（`deploy/paddleocr/`，RTX 5090 GPU）已接为流水线
一等后端，并设为 `--ocr-backend auto` 的默认优先选择：**本地部署可用时
一律走本地 GPU 推理**（无内容过滤、不耗 API 配额），不可用时自动回退
到 OCR profile（云端 coding-plan/glm_vision）。显式 `--ocr-backend
coding-plan-mcp` 等仍可强制远端；适合整本本地 OCR、续跑失败页，以及云端
视觉模型内容过滤拒绝的页面：

```bash
# 默认即本地优先（auto）：可用时走本地 GPU，否则自动回退云端，无需任何参数
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr

# 强制本地（检查点指纹为 paddleocr-local/PP-OCRv5-…，与云端检查点互不混用）
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr   --ocr-backend paddleocr-local

# 强制云端（备用）
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr   --ocr-backend coding-plan-mcp

# 只补失败页（自动按连续页段批量调用 Docker，已匹配指纹的页直接命中缓存）
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr   --start-page 198 --end-page 198 --ocr-backend paddleocr-local

# 或经 Profile 选用（pipeline.toml 已内置 profiles.paddleocr_local）
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr   --config pipeline.toml --ocr-profile paddleocr_local
```

执行体是 `tools/local_paddleocr_import.py`（渲染 → `docker compose --profile
gpu` 分片识别 → 导入 PageRecord），参数用 `--paddle-det-variant/--paddle-rec-
variant/--paddle-det-mode/--paddle-rec-mode/--paddle-rec-batch/--paddle-det-len/
--paddle-workers` 调整；先决条件：Docker Desktop 已启动、`deploy/paddleocr/
models/` 权重已下载（见 `deploy/paddleocr/README.md` 实测配置）。空白页沿用
`[空白页]` 显式标记约定，跨页断句、目录与出版流程与其他后端完全一致。

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

1. 把 PDF 每页渲染为临时 JPG，通过 Coding Plan 视觉 MCP 明确调用 GLM-4.6V，按 `--ocr-concurrency` 并行保存逐页 JSON；日文竖排的横向双页扫描会先沿书脊拆成右页、左页，超时或 1301 内容过滤时继续沿空白列/行细分，避免机械重试同一密集整页；
2. 将前 40 页 OCR 文本交给 GLM 判断目录页并输出目录 JSON；
3. 在目录之后寻找章节标题，自动判断每个 PDF 页包含 1 或 2 个书内页，并计算页码映射；
4. 按章生成以标题组织、不含来源页码的 Markdown；
5. 如开启翻译，以独立 DeepSeek API 按 `--translation-concurrency` 并行翻译非中文页，再通过发布清洗层生成不含原始分页和来源页码的 EPUB、Word、知识库 JSONL，同时生成带书签参考 PDF。

逐页 OCR 和逐页翻译都是可分片的模型阶段，分别使用独立 worker 池并尽可能并行；目录发现与结构化以整本书的候选目录文本为一个整体请求，必须等 OCR 检查点齐备后执行，不做会破坏上下文一致性的切片并发。两个 worker 数都可以按各自服务的速率限制独立调整。

Coding Plan OCR 的请求节流按 API Key 指纹在本机进程间共享：同时处理多本书时，各进程不会分别占满一套额度。初始请求间隔由 `CODING_PLAN_VISION_REQUEST_DELAY` 控制；每连续成功 `CODING_PLAN_VISION_SPEEDUP_WINDOW` 次会小幅缩短间隔，遇到 429 则立即延长，范围由 `CODING_PLAN_VISION_MIN_REQUEST_DELAY` 和 `CODING_PLAN_VISION_MAX_REQUEST_DELAY` 限定。Key 本身不会写入共享状态或日志。worker 数控制在途页面数，自适应间隔控制请求启动速率，两者作用不同。

逐页 OCR 结果会自动断点续传；稳定输出目录可重复使用。`ocr_model` 同时记录模型、阅读方向和提示词版本（例如 `coding-plan/glm-4.6v-vision-mcp/vertical-v2`）。Graph 默认把推导出的完整值作为 `--ocr-cache-model` 精确匹配，模型、方向或提示词版本升级后会自动刷新不相符的检查点；`--ocr-cache-model-prefix` 只用于经过人工确认的旧缓存兼容，不是刷新开关。日志会输出每页开始、响应耗时和字符数，页级 `notes` 也保存耗时。临时页图默认删除，使用 `--keep-page-images` 可保留以便复核。分段补救会丢弃空白带返回的“无可见文字”说明，并以 `mcp-segmented-*` 请求标记保留审计线索；MCP stderr 会被脱敏后附到错误中，超时关闭整个 npx/Node 进程组，不遗留占用连接的孤儿进程。如果单列/行本身仍被服务拒绝，应只对该页使用本地 OCR 并对照原图复核，不能留下缺页或把过滤说明混入正文。

## 推荐的可审计分步流程

### 第一步：逐页 OCR

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase ocr
```

OCR 结果位于 `outputs/my_book/pages/page_XXXX.json`，并同步生成便于人工复制的 `page_XXXX.md`。每页记录包含 PDF 页码、Markdown 文本、语言检测、可选译文和 OCR 模型；双页扫描的 JSON 还包含结构化物理页数组。

如果 PDF 的每一页都有完整的可复制文本层，可跳过视觉 OCR，使用通用导入器：

```bash
python extract_textbook_layer.py "book/input.pdf" -o "outputs/my_book" \
  --strip-leading-page-number-offset 1 --reflow
```

也可让 Graph 以第三入口一次执行后续翻译、编译、发布与验收：

```bash
python graph_pipeline.py "book/input.pdf" -o "outputs/my_book" --phase all \
  --source-mode text-pdf --text-pdf-reflow --translate-non-chinese \
  --recipe recipes/text-pdf-full-publication.toml
```

`--text-pdf-sort` 和 `--text-pdf-strip-leading-page-number-offset N` 对应独立
导入器的同类选项；它们仅在显式 `text-pdf` 模式有效。任一正文页缺少文字层时，
节点会在写入检查点前阻断并提示改用默认扫描 PDF/OCR 入口；纯空白的首尾页会
写成显式空白检查点，以保持逐页来源覆盖，但内部空页仍会阻断待复核。

导入器会先验证整本 PDF，再写入 `PageRecord` 检查点；任一非首尾空白页没有
文本层（包括只有图片的页面）都会在写入前停止。重复导入相同文字会保留新鲜译文；
文字变化时默认拒绝，只有显式传入 `--force` 才替换变化页并使其旧译文失效。
默认使用 PDF 的逻辑内容顺序；`--sort` 可改用视觉位置排序，`--reflow`
则按空行分段并合并段内视觉换行。页码清理默认关闭，而且只会删除严格等于
“PDF 页码减指定偏移”的首个独立数字行。

可选的 `--headings-json headings.json` 接受标题映射或对象数组，例如：

```json
[
  {
    "title": "Chapter One",
    "level": 2,
    "aliases": ["CHAPTER ONE"],
    "replacement": "第一章",
    "pdf_page": 10
  }
]
```

只有在全书中作为完整独立行且唯一匹配的标题才会添加 Markdown 标记；
可选 `replacement` 会在唯一命中后输出审定标题，缺省则保留匹配到的原文。
目录与正文重复出现同一标题时，可用正整数 `pdf_page` 将唯一匹配限制到正文页。
缺失或重复匹配只报告清单，不进行模糊猜测。通用导入器不再生成旧版脚本中
针对单本日文书硬编码的 `toc.json`，导入后应运行 `book_pipeline.py --phase toc`
或提供 `--toc-json`。编译前可用
`--required-ocr-model-prefix text-layer/pymupdf-v1` 验证来源。

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

`printed_page` 是目录中印出的书内页码；程序会比较单页扫描与双页扫描证据，并按 `PDF页 = floor(书内页 / 每PDF页书内页数) + 偏移` 写回 `pdf_page`。OCR 中高置信度的章、节、前言和结语标题页可逐项覆盖公式映射。缺少可靠证据时会停止并要求传入 `--printed-pages-per-pdf-page` / `--page-offset`，不会静默猜测；显式参数始终优先。

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

单页扫描按章时使用不重叠页区间。双页扫描如果 checkpoint 带有物理页数组，编译器会用 `printed_page % 2` 精确选择右/左半页，同一 PDF 页分属两章时不再复制全页。旧 checkpoint 没有该数组时仍使用闭区间和下一标题裁切，无需迁移。小节粒度使用 `--granularity subsection`，`chapters.json` 会记录 `boundary_mode` 供审计。

所有章节文件都以标题编译：

```markdown
# 第一章 复调小说与陀思妥耶夫斯基创作

章节正文……
```

EPUB 导航、Word 标题和 PDF 书签均从这些结构化标题生成，而不是从正文猜测。

章节 Markdown、EPUB、Word 与知识库 JSONL 都是阅读/检索发布层，不保留来源字段、原 PDF 分页锚点、书内页码或内部物理页边界。可审计坐标仍保留在逐页 JSON、`toc.json` 和 `chapters.json` 中。可单独重新生成 Word：

```bash
python book_pipeline.py -o "outputs/my_book" --phase docx --title "书名"
```

### RAG 知识库与 embedding 接口

知识库发布器保留经过发布验收的 `knowledge_base.jsonl` 作为 canonical 文档语料，
并自动生成 `knowledge_base.rag.json`。在尚未配置 embedding API 时，RAG 运行时
使用内置 Okapi BM25 检索；接入 API 后，向量单独写入
`knowledge_base.vectors.jsonl`，不会把向量字段混入正文 JSONL 或破坏稳定 ID、
全文覆盖与发布验收契约。

本分支已内置智谱 OpenAI 兼容 provider，默认使用
`https://open.bigmodel.cn/api/paas/v4/`、`embedding-3` 和 2048 维向量。
密钥只从环境变量读取，不写入源码或 RAG 产物：

```powershell
$env:ZHIPU_API_KEY = "在智谱控制台生成的密钥"
```

构建向量索引并检索带 chunk 引用、可直接注入生成提示词的上下文：

```python
from rag_knowledge_base import ZhipuEmbeddingProvider
from translation_agent_api import (
    build_knowledge_base_embedding_index,
    retrieve_knowledge_base_context,
)

provider = ZhipuEmbeddingProvider()  # embedding-3 / 2048 dimensions
build_knowledge_base_embedding_index("outputs/my_book", provider)
context = retrieve_knowledge_base_context(
    "outputs/my_book",
    "作者如何界定文化领导权？",
    top_k=5,
    embedding_provider=provider,
)
print(context.text)
```

若暂时不传 `embedding_provider`，`retrieve_knowledge_base_context` 会自动使用
BM25 回退，因此当前阶段无需任何联网依赖或额外向量数据库。
智谱单次请求最多提交 64 条输入，索引器默认按该上限分批；仅安装 core
依赖时需使用 `pip install '.[legacy]'` 安装已有的 OpenAI 兼容客户端。

仓库内也提供统一 CLI，方便把成品目录注册成可检索 RAG 产物：

```bash
translation-agent-kb register "outputs/my_book"
translation-agent-kb retrieve "outputs/my_book" "作者如何界定文化领导权？" --semantic
translation-agent-kb status "outputs/my_book"
```

`register` 默认使用智谱 `embedding-3` / 2048 维并读取 `ZHIPU_API_KEY`；
如只想生成清单和 BM25 回退，可加 `--lexical-only`。`retrieve --semantic`
只有在向量索引已就绪时才调用 embedding provider，否则自动回退 lexical 检索。
临时 DOCX 成品可用 Heading 1 章节结构派生知识库：

```bash
translation-agent-kb derive-docx "outputs/Book.docx"
```

该命令会自动创建同名产物目录并写入 `knowledge_base.jsonl` 与 RAG 清单；如果
DOCX 没有可识别的 Heading 1 章节契约，会直接失败而不是生成不可审计的语料。

#### 混合检索、查询路由与每书上限

多书合辑库（语料不均衡、大书淹没小书、单一排序偏差）默认使用
**BM25＋向量 RRF 融合**（`--mode hybrid` 为默认）：两通道各取
`--candidate-depth`（默认 30）候选，按 Reciprocal Rank Fusion 合并，命中方式
（`lexical` / `semantic` / `lexical+semantic`）随每条结果返回；embedding 索引
不可用时自动降级 lexical。查询会识别明确出现的书名及作者，并在排序前自动
缩小语料范围；显式 `--book` / `--author` 始终优先，`--no-auto-route` 可关闭
自动路由。比较型问题中提及的书籍与作者所属书籍取并集，不会被错误地求交集。
非比较问题同时出现书名和作者时，明确书名优先，以免把该作者的其他著作一并
混入。RRF 的 `--candidate-depth` 必须不小于 `--top-k`。
查询路由与结果均衡：

```bash
# 限定某书 / 某作者 / 某语言（元数据侧表 knowledge_base.meta.jsonl 提供）
translation-agent-kb retrieve "outputs/合集" "共同幻想与国家" --book 共同幻想論
translation-agent-kb retrieve "outputs/合集" "奥姆之后的日常" --author 宮台真司
translation-agent-kb retrieve "outputs/合集" "幻想論" --language ja

# 每书结果上限（默认 3，防止 542 块的大书淹没 23 块的小书；0 关闭）
translation-agent-kb retrieve "outputs/合集" "丸山真男" --per-book-cap 2

# 查看未路由的全库结果
translation-agent-kb retrieve "outputs/合集" "日本思想" --no-auto-route
```

过滤后只有一本书时，每书上限自动取消，`--top-k` 因此仍可返回该书的完整候选。

侧表是可选的 `knowledge_base.meta.jsonl`（每行 `id` + `book_id`/`book_title`/
`author`/`language`/…），不破坏五字段主语料契约；没有侧表时按合辑惯例从
`chapter_id` 的书名前缀（`01_书名:章`）或 `[书名]` 标题前缀推导书身份。
`translation-agent-kb status` 会报告侧表行数和覆盖率；重复ID或已不属于当前
主语料的陈旧ID会使状态校验失败，避免错误路由静默生效。
上下文前缀同步展示 `[KB:id] [书名] 标题 (命中通道)`。

多册合集 EPUB（鲁迅全集、王小波作品大全集等）可用合集拆分构建器按目录
拆成单独作品后注入知识库；`book_title` 侧表使检索按作品路由、每书上限
防止大部头淹没短章：

```bash
python tools/books/epub_collection_kb.py "book/合集.epub" \
  --output-dir "outputs/知识库_合集" --author 作者 --language zh --sources
translation-agent-kb register "outputs/知识库_合集"
```

拆分规则：目录嵌套解析卷→作品→篇目（纯数字续篇归并回前一部作品），
未编目书脊文件按就近归属并入前一作品（`[n]` 脚注块并入前一篇），长篇
在段落边界切分为 ≤4000 字块，与合辑库分块契约一致。

### 发布质量门

`compile` 和 `all` 在生成产物后自动运行一次无模型调用的发布质量门；任何
失败都会使命令返回非零状态，并把机器可读报告写到
`audit/release-report.json`。也可独立运行：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase verify --report "outputs/my_book/audit/release-report.json"
```

修复单个问题章节后，先做一次只更新 Markdown 的轻量本地编译，再用 manifest
中的 ID 或十进制 `sequence` 做快速增量检查；`--chapter-id` 仅能与
`--phase verify` 一起使用。不必反复生成或解析 EPUB、Word、知识库和整本 PDF：

```bash
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase compile \
  --no-verify --no-epub --no-docx --no-kb --no-bookmarked-pdf
python book_pipeline.py "input.pdf" -o "outputs/my_book" \
  --phase verify --chapter-id chapter-5 --chapter-id chapter-6 \
  --report "outputs/my_book/audit/chapter-report.json"
```

全部问题章通过后运行一次正常 `compile` 即可；它生成全部格式并自动完成全书
验收，不需要立即再重复运行一次 `verify`。

整本均已人工审定时可加 `--require-all-reviewed`。报告会从当前产物动态统计
章节、正文引注/尾注、知识库块、PDF 页和书签数量，不使用历史书籍的固定
数字。只有退出码为 0，报告 `mode=full`、`ok=true`、`release_ready=true`、
`status=passed`、`summary.skipped=0`，且 warnings 已人工判断并披露，才表示
所有格式可完整发布；主动跳过任一格式会得到 `partial` 报告和非零退出码。
纯书籍内容修订只需运行章节增量门与最终全书门；全套单元测试留给框架代码
发生变化时运行。

完整书签/PDF、全页 OCR 覆盖和知识库稳定 ID 验收必须传入真正的源 PDF；
省略源文件并加 `--no-bookmarked-pdf` 只能做 partial 检查，不能声明完整发布。
最终 compile 建议加 `--require-complete-ocr`，翻译任务再加
`--require-translation`，以便在生成容器前尽早失败。未引用的纯注释清单会作为 warning 保留，
需要人工确认；采用引注体系的审定章中，缺注、孤儿注释和重复定义都会直接
阻止发布。全书门还会逐章比对 EPUB/Word/知识库可见全文及标题签名，精确核对
Word 表格、引文归属、粗体/斜体/下划线，并将带书签 PDF 的页面几何、文字层
和低分辨率 RGB 外观逐页与源 PDF 比对，而不只检查数量。

### 章节注释重组（tools/note_reflow.py）

当章节出现"注释定义散布在正文段落之间、正文被逐条打断"时（常见于从
Word/LaTeX 导出的论文 PDF），先在审定稿层面重排，再走常规增量门+全书门：

```bash
# 1. 将问题章节放入 reviewed_chapters/（人工审定稿）
# 2. 预览统计：定义/引用数量、闭环缺口（不写盘）
python tools/note_reflow.py --dry
# 3. 写回审定稿：定义归入章末 ## 注释，正文引用标记化为 〔n〕
python tools/note_reflow.py
# 4. 轻量编译更新发布章
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase compile \
  --no-verify --no-epub --no-docx --no-kb --no-bookmarked-pdf
# 5. 增量门验收改动章
python book_pipeline.py "input.pdf" -o "outputs/my_book" --phase verify \
  --chapter-id intro --chapter-id ch-1 \
  --report "outputs/my_book/audit/chapter-report.json"
# 6. 全部通过后正常编译一次，自动跑全书门
```

工具要求 `--dry` 输出中每章"定义数 == 引用唯一数"（双向闭环）后才写盘；
多章之间注释编号全书连续时（如 1–162 → 163–306 …），各章定义可直接沿用
原编号，正文引用与章末定义一一对应。

## 非中文 OCR 的 DeepSeek 翻译接口

程序总会保留原始 `text`。检测到日文、英文、韩文、俄文等非中文文本时，可显式开启翻译。独立的 `translate` 阶段不依赖 `toc.json`，因此可以在 OCR 仍在写入逐页检查点时分批运行：

```bash
python book_pipeline.py -o "outputs/my_book" \
  --phase translate \
  --config pipeline.toml --translation-profile deepseek_flash \
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
  --config pipeline.toml --translation-profile deepseek_flash \
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
        translation_profile="deepseek_flash",
        translate_non_chinese=True,
        source_language="ja",
        require_complete_ocr=True,
        require_translation=True,
        verify_publication=True,
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
├── audit/
│   ├── semantic-reconstruction.json # 引用落点及阻断问题审计
│   ├── word-release-report.json      # Word Recipe 的正式验收报告
│   └── release-report.json           # 完整多格式发布报告
├── knowledge_base.jsonl      # 仅含章节、顺序和正文的无分页 RAG 记录
├── knowledge_base.rag.json   # RAG 语料哈希、检索能力与 embedding 状态
├── knowledge_base.vectors.jsonl # 接入 embedding API 后生成的向量 sidecar
├── 书名.epub                  # 无原 PDF 分页信息的 EPUB3
├── 书名.docx                  # 无原 PDF 分页信息的 Word 文档
└── 书名_带目录.pdf            # 原版外观 + 可复制层（若原有）+ 书签
```

## 常用参数

```text
--phase all|ocr|proofread|translate|toc|compile|epub|docx|verify|status
--config pipeline.toml       Provider/Profile 配置
--ocr-profile NAME           覆盖 OCR Profile
--toc-profile NAME           覆盖目录文本 Profile
--proofread-profile NAME     覆盖 OCR 校勘 Profile；默认复用 translation_profile
--translation-profile NAME   覆盖翻译 Profile
--toc-pages 6-10,12          人工指定 PDF 目录页
--toc-json FILE              使用人工目录 JSON
--page-offset N              人工指定 PDF 页码减书内页码
--printed-pages-per-pdf-page 1|2  单页或双页扫描；默认根据章节标题自动检测
--api-mode coding-plan|standard
--ocr-backend coding-plan-mcp|glm-ocr|tesseract
--ocr-concurrency 4          逐页 OCR worker 数
--ocr-reading-direction horizontal|vertical  OCR 与分段阅读方向；日文竖排用 vertical
--ocr-cache-model-prefix coding-plan/  仅复用指定模型来源，自动覆盖本地兜底页
--proofread-language ja      校勘原文语言（当前提示词用于日文 OCR）
--proofread-concurrency 8    逐页校勘 worker 数
--proofread-delay 0          worker 间请求启动间隔秒数
--proofread-max-chars 12000  单个校勘分块最大字符数
--tesseract-language jpn_vert+eng  Tesseract 语言包；横排日文用 jpn+eng
--tesseract-psm 3             Tesseract 页面分割模式
--granularity chapter|section|subsection|all  省略时保持现有 manifest 粒度，首次默认 chapter
--translate-non-chinese      仅翻译检测为非中文的页
--translation-source-language auto|ja|en  覆盖自动语言检测
--translation-provider deepseek   翻译后端（默认 deepseek）
--translation-api-key-env NAME  从指定环境变量读取翻译 Key
--translation-api-base URL   DeepSeek OpenAI 兼容端点
--translation-model NAME     翻译模型（默认 deepseek-v4-flash）
--translation-api-timeout 120  翻译 API 单次请求超时
--translation-max-chars 12000  单个翻译分块的最大字符数
--translation-concurrency 16  逐页翻译 worker 数
--start-page / --end-page    局部 OCR、翻译或联调
--force                      重做已有 OCR/翻译检查点
--chapter-id ID|SEQUENCE     仅用于 verify 的增量验收，可重复
--require-all-reviewed       要求整本每章都有人工审定稿
--report FILE                指定发布验收 JSON 报告路径
--no-verify                  compile/all 后跳过自动验收（不建议交付时使用）
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
