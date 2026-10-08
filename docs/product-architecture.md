# 产品架构与迭代边界

本页描述当前可安装产品的公共边界。实现保持 local-first：单机文件、SQLite WAL
和 JSONL 足以支持长任务恢复；不依赖 Celery、Kubernetes 或远程数据库。

## 组件图

```mermaid
flowchart TB
    CLI["translation-agent CLI"] --> APP["Application / RunSpec service"]
    UI["Streamlit WebUI"] --> APP
    APP --> JOBS["UUID workspace + SQLite WAL jobs"]
    APP --> ADAPTER{"explicit Source Adapter"}
    ADAPTER -->|"scanned-pdf"| OCR["PDF OCR Graph"]
    ADAPTER -->|"text-pdf"| TEXT["PDF text-layer Graph"]
    ADAPTER -->|"epub"| EPUB["EPUB spine semantic importer"]
    OCR --> DAG["PipelineGraph executor"]
    TEXT --> DAG
    EPUB --> SOURCE["document semantic source / translation units"]
    DAG --> SOURCE
    SOURCE --> TRANSLATE["Provider + semantic translation QA"]
    TRANSLATE --> APPLY["transactional semantic apply"]
    APPLY --> SANITIZE["reader sanitize"]
    SANITIZE --> PUBLISH["EPUB / DOCX / KB / reference PDF"]
    PUBLISH --> VERIFY["package + render + release report"]
    VERIFY --> CATALOG["identity-bound artifact catalog"]
    CATALOG --> UI
```

EPUB 当前能完成导入、语义翻译、事务回填和草稿发布，但尚未注册与 PDF 等价的
EPUB-native release profile。因此产品壳不会给 EPUB 草稿签发
`release_ready=true`。

## 数据管线与中文质量门

下图是当前完整数据管线。★ 标记的是中文语言质量门（2026-09 新增）：所有进入
数据库（全局知识库 reader 层）与 Word 成品的外文正文必须先翻译为中文；
目录/索引/对照表等双语装置与短尾块按规则豁免。

```mermaid
flowchart TB
    subgraph IN["① 源接入（显式 Source Adapter）"]
        SPDF["扫描 PDF"] --> OCRSEL{"--ocr-backend<br/>auto（默认）"}
        TPDF["文本层 PDF"] --> TEXT["PDF 文本层 Graph"]
        EPUBF["EPUB"] --> EIMP["EPUB 语义导入<br/>例：畏怖する人間（36 章 → 70 块）"]
    end

    subgraph DK["本机视觉层 · Docker GPU 容器（书页图像不出机器 · 不消耗 API 配额）"]
        IMG["local/paddleocr:3.7.0-gpu<br/>48.3 GB · RTX 5090 D sm_120<br/>paddle 3.3.0-gpu-cuda12.9"]
        WGT["挂载 deploy/paddleocr/models<br/>PP-OCRv5 server + mobile（193 MB）"]
        IO["挂载 deploy/paddleocr/io<br/>文件式请求 / 结果交换"]
        IMG --- WGT
        IMG --- IO
        IO --> OCRP["PaddleOCR 竖排/横排推理<br/>例：私小説論（376 页 → 51 块）"]
    end

    OCRSEL -->|"docker 引擎 + 镜像 + 权重齐备"| OCRP
    OCRP -->|"每段连续页一次 docker run<br/>tools/local_paddleocr_import.py<br/>checkpoint 身份 paddleocr-local/&lt;fingerprint&gt;"| DAG
    OCRSEL -->|"本地不可用 → fail-closed 报错终止<br/>（云端补页分支已删除）"| STOP["OCR 阶段失败<br/>不发送页图"]
    OCRSEL -->|"显式离线引擎"| TESS["Tesseract（本地）"]
    TESS --> DAG

    DAG["PipelineGraph 执行器<br/>声明输入 · 深度隔离 · 缓存指纹"]
    TEXT --> DAG
    EIMP --> SEM["document semantic source<br/>章节 · 块 · 翻译单元"]
    DAG --> SEM
    SEM --> TR["Provider 翻译 + 语义 QA"]
    TR --> AP["事务性语义回填 semantic_apply"]
    AP --> SZ["reader sanitize"]
    SZ --> PB["发布器"]

    PB --> CH["chapters/*.md + chapters.json"]
    PB --> EO["EPUB 成品"]
    PB --> DO["Word 成品 DOCX"]
    PB --> PO["带目录 PDF"]
    CH --> KR["build_knowledge_rows<br/>split_text ≈4000 字 · 稳定 SHA-1 id"]
    KR --> KBJ["knowledge_base.jsonl<br/>五字段权威语料"]
    KBJ --> RG["RAG 旁车：apparatus / rag.json<br/>vectors（zhipu embedding-3）/ meta"]

    CH --> VF["publication_verifier<br/>发布校验（13 项检查）"]
    KBJ --> VF
    DO --> VF
    EO --> VF
    PO --> VF
    VF --> LANG1{"★ knowledge_base.chinese<br/>★ docx.chinese"}
    LANG1 -->|"外文正文 → failed"| TRK["translation-agent-kb translate-kb<br/>docx_translation.translate_docx<br/>（含备份与还原）"]
    TRK --> VF
    LANG1 -->|"全部中文或豁免"| RR["release-report.json<br/>release_ready"]

    KBJ --> GS["global_knowledge_base sync<br/>变化工作区增量同步 · 临时快照 · 原子替换"]
    GS --> LANG2{"★ require_chinese<br/>（默认开启）"}
    LANG2 -->|"外文 reader 块 → 中止<br/>列出工作区清单<br/>--allow-foreign 临时绕过"| GS
    LANG2 -->|"通过"| GDB[("global_knowledge_base.sqlite3<br/>schema v4 · 三元组/短词倒排<br/>reader / pages / archive 三层")]
```

语言门的判定与豁免规则（离线，无网络调用）：

```mermaid
flowchart LR
    subgraph JUDGE["离线判定"]
        CLS["kb_translation.classify_row<br/>detect_language + 豁免规则"]
        DXC["docx_translation.needs_translation<br/>假名 / 拉丁计数"]
    end
    CLS -->|already_chinese| PASS["放行"]
    CLS -->|"reference_material（索引/对照表/书目）<br/>not_prose（页码/版权行）"| PASS
    CLS -->|non_chinese_body| NEED["需翻译"]
    DXC -->|"中文 / 页码 / 版权行"| PASS
    DXC -->|"假名出现 / 拉丁正文 ≥ 12 字符"| NEED
    NEED --> ENF["三个强制点：<br/>① 发布门 knowledge_base.chinese<br/>② 发布门 docx.chinese<br/>③ 全局库 sync 门（meta: chinese_gate）"]
    ENF -->|"翻译后重检"| PASS
```

补充说明：

- **视觉层是本机 Docker GPU 容器，且已 fail-closed**：`--ocr-backend auto` 只选本地
  PaddleOCR（探测 `docker info` + 镜像 + 权重目录，不启动 GPU）；本地不可用时
  OCR 阶段直接报错终止（"vision executes locally only"），**不再把页图发往云端**。
  云端后端 `coding-plan-mcp` / `glm-ocr` 已从 CLI choices、构造分支、缓存身份解析器
  与 graph 阶段语义中移除，显式传入在参数解析层即被拒绝；`tesseract` 是唯一保留的
  离线替代引擎。非 OCR 阶段（status/checkpoint 内省）在容器停着时按本地缓存身份
  运算，只读不外发。
- 镜像 `local/paddleocr:3.7.0-gpu`（48.3 GB，paddle 3.3.0-gpu-cuda12.9，RTX 5090 D
  sm_120），权重与 IO 目录经挂载卷交换文件；宿主对每段连续页发起一次
  `docker run`（`tools/local_paddleocr_import.py`）。页面 checkpoint 身份包含本地
  指纹（`paddleocr-local/<model fingerprint>`）。
- **本地/云边界要说清**：不出机器的是**页图与视觉推理**；文本阶段仍调用云端——
  目录抽取 `glm-5.2`、校对与页面翻译 `deepseek-v4-flash`。因此 API 密钥与配额
  只影响文本阶段，OCR 不消耗配额；embedding 缺失时会降级为纯词法 KB 而不是
  阻断发布。
- `pages` 层（原页 OCR / 校对文本）与 `archive` 层（章节快照）按设计保留原文，
  不在语言门内；`page_translation` 层本身就是对应译文。门只约束检索面（reader 层）
  与交付物（Word）。
- 全局库 meta 的 `chinese_gate` 记录本次构建是 `enforced` 还是 `allowed`
  （`--allow-foreign` 绕过时为后者），`status` 命令可直接查看。
- 翻译步骤通过 `knowledge_base.translation.json` 旁车绑定语料字节并提供
  `translation-source.jsonl` 原文备份，可整体还原；翻译会使向量旁车失效，
  需重新 `register`。批次失败（标记错位或受保护占位符丢失）按既有阶梯重试并
  降级为单段请求，不会因一个坏回包中止整本书。

## 公共契约

| 契约 | 版本 | 责任 |
| --- | --- | --- |
| `RunSpec` | v1 | 统一 CLI、服务与 UI 的输入；严格拒绝未知字段，不保存密钥 |
| `DocumentSemantic` | v1 | 文档、章节、块、来源定位、翻译单元与人工决定 |
| `ArtifactRecord` | v1 | 文件身份、SHA-256、草稿/阻断/正式状态 |
| `RunEvent` | v1 | 可序列化任务事件与错误摘要 |
| Graph state/event | v1 | 节点缓存、产物指纹和恢复记录 |

Provider、Source Adapter 和 publisher 可以迭代，但不能绕过这些契约。新的字段需
先增加 schema 迁移；不能让 UI、CLI 和 Graph 分别解释不同的配置。

## DAG 不变量

1. 节点只能读取 `NodeSpec.requires` 中声明的值；未声明访问立即失败。
2. 节点收到深度隔离的输入副本，不能修改上游嵌套对象；执行前后会复核输入指纹。
3. draft semantic audit 与 reader semantic audit 是不同产物。sanitize 只能提供新的
   reader 证据，不能改写 `chapters.semantic`。
4. 缓存身份包含节点名、版本、显式 fingerprint 和依赖产物指纹；Provider 缓存还
   包含 provider、endpoint、model、prompt profile、thinking、temperature 与术语表。
5. 发布器只消费通过的 reader semantic bundle。裸 `publication.docx` 是中间产物；
   正式 Word 目标是 `publication.word_report`。

## 语义回填不变量

`semantic_apply.py` 是 EPUB 与独立文字 PDF importer 的共享 apply 服务：

- 上游 reconstruction audit 必须明确 `passed`，且不能有 root/chapter blocker；
- unit schema、ID、章节顺序、source SHA 和 locator 必须完整一致；
- 标题层级、列表、表格、链接目标、脚注引用—定义关系不能被模型改变；
- 目标语言、普通英文残留和 glossary 在回填前复核；
- 所有章节、manifest 和 translation audit 先写 staging；提交失败回滚；
- reconstruction audit 保持不可变，新的 translation audit 记录其 SHA-256。

人工复核记录使用 append-only `review-decisions.jsonl`，决定绑定 issue/unit/source
hash；来源改变后旧决定自然失效。

## WebUI 安全模型

- 只绑定 `localhost`、`127.0.0.1` 或 `::1`；远程访问使用带认证的隧道。
- 文件路径必须位于配置的 allowlist 根目录；上传有扩展名、文件名和大小门。
- 每个任务拥有 UUID 工作区，数据库使用 SQLite WAL；支持取消、恢复和断点复用。
- 子进程环境采用 allowlist；模型凭据只在内存中传递，不进 RunSpec/SQLite/argv。
- 产物页只把 Graph state 中登记、且 release report 身份与哈希匹配的文件标为正式。

默认运行根为 `~/.translation-agent/webui/`，属于用户级本地状态，不应提交到 Git；
可通过 `TRANSLATION_AGENT_WEBUI_RUNTIME_ROOT` 覆盖。源码运行优先读取仓库内
的 Profile/Recipe，wheel 安装则从 `sys.prefix/share/translation-agent/` 读取
同一组受信资源。

## 轻量迭代流程

```text
改 schema/契约 → 写迁移或兼容读取 → 增加跨 adapter fixture
             → Graph plan / cache 回归 → package/render gate → 发布版本
```

每次合并前至少运行：

```bash
translation-agent-repo-guard
python -m unittest discover tests
python -m compileall -q .
python -m build
```

CI 在 Python 3.11 和 3.12 上执行相同快速门；需要 LibreOffice 与固定字体的真实
渲染 smoke 可放在 nightly/release job，避免拖慢日常小改动。
