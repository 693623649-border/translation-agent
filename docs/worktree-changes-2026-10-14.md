# 133f worktree 改动整理（2026-10-14）

基线为 `44dd0ff`，整理开始时处于 detached HEAD。以下覆盖当时的 17 个已修改文件及所有未跟踪文件类别；不改写既有书稿或模型原始响应。

## 可提交的产品改动

| 改动组 | 文件 | 行为与边界 |
| --- | --- | --- |
| 分语言翻译提示词 | `paragraph_translation.py`、`book_pipeline.py` | 本地日文使用用户已选的 V12 模板；英文使用独立模板；总缓存版本升至 v13，telemetry 记录语言模板。local 段落门放宽普通标点签名，保留引号、语言残留等检查；云端日译中维持严格标点门。 |
| 发布检查点 | `publication_verifier.py` | v11、v12、v13 都核对邻页上下文输入哈希，避免上下文改变后沿用旧检查点；正式发布的其他验收仍独立。 |
| 全库正式检索 | `knowledge_base_cli.py`、`rag_knowledge_base.py` | CLI 全库 hybrid 排除未就绪向量书库并披露部分覆盖，provider 失败不静默降级；全库默认关闭自动书籍路由，单书默认保留。相对路径区分嵌套同名库，精确书籍过滤优先，命中附书名、作者、来源路径和重复来源。底层兼容路径仍由 `strict_hybrid` 显式控制。 |
| 本地部署与 profile | `.env.example`、`pipeline.toml`、`pipeline.example.toml`、`deploy/local-llm/docker-compose.yml` | profile 统一为 `local_llm`；本地默认翻译，示例仍默认 DeepSeek；校勘单独配置。vLLM 镜像改为 v0.31.0，权重和缓存使用命名卷，显存预算 0.70、序列上限 16、eager 执行；只绑定回环端口。 |
| 译文证据采集 | `deploy/local-llm/smoke_translate.py`、`deploy/local-llm/evidence/2026-10-14/` | 保存请求正文、原始响应、失败重试、流水线产物及哈希，不记录鉴权头。证据目录 55 文件、120037 字节，包含 15 条历史响应、3 个样例的 4 次采集响应及当时验证快照；人工译文验收未自动签署。 |
| 回归覆盖 | `tests/test_knowledge_base_cli.py`、`tests/test_local_translation_provider.py`、`tests/test_paragraph_translation.py`、`tests/test_pipeline_profiles.py`、`tests/test_rag_multi_book.py`、新增 `tests/test_local_translation_evidence.py` | 覆盖语言分流、历史模板比对、provider 门禁、profile、全库 strict hybrid、嵌套来源、书籍过滤、失败响应留证及凭据不入报告。 |
| 操作与历史说明 | `README.md`、`deploy/local-llm/README.md`、新增 `docs/ocr-sampling-and-vision.md`、`docs/translation-language-prompts.md`、`docs/workflow-history.md` | 按准备、导入、翻译审校、发布、注册、索引、检索组织操作；区分 OCR 视觉核图与文本校勘，明确非连续样本必须逐页限定；历史记录与当前操作分开。 |
| IDE 排除配置 | 新增 `.zcodeignore` | 已有的 ZCode 排除规则：环境、产物、缓存、工具及依赖目录不参与 IDE 索引。它不是 Git 排除规则。 |

## 本轮新增的协作规则与整理

- 根目录新建 `AGENTS.md`：任务改动经过审查与必要验证后自动本地 commit；只提交已审查范围，不夹带无关工作、密钥和本地书稿；失败时修复或报告阻断。push、合并和发布仍需对应授权。
- `.gitignore` 新增 `.dsh/`、`dsh-session-*.zip`、`/deploy/xiaomi-ocr/verify_page.png`，保留原文件，避免本机核图、会话和书籍材料进入源码提交。
- `.gitattributes` 为部署证据目录禁用 Git 自动换行转换，保留原始响应与历史脚本的字节和 SHA256 身份。
- 本文记录全部改动类别、提交范围和验证限制。

## 保留本地且不提交的材料

| 路径 | 清点 | 处理原因 |
| --- | --- | --- |
| `.dsh/` | 共 199 文件：167 PNG、18 TXT、9 Python、3 JSON、1 JSONL、1 Markdown；156081551 字节 | 包含 `kana-fix` 书籍修复、`spinoza2` OCR/几何方案/核图、知识库摘录与本地 skill 副本，是个人任务材料；未逐页或逐脚本做出版/产品验收。 |
| `dsh-session-session-d70952d9-b05a-4128-b2d9-732c5241e3d1.zip` | 1243889 字节 | 会话归档；提交可复查的抽取证据，原始会话留在本机。 |
| `deploy/xiaomi-ocr/verify_page.png` | 49390 字节 | 本机 OCR 单页验证截图；不是本轮产品源码或已验收发布物。 |

上述材料没有删除、移动或改写。可用 `git status --short --ignored .dsh/ 'dsh-session-*.zip' deploy/xiaomi-ocr/verify_page.png` 查看其保留状态。

## 本次验证及限制

- `python -m unittest discover -s tests -q`：981 项，52.580 秒，`OK (skipped=26)`，退出码 0。跳过项不计作运行通过。
- 临时独立 worktree 仅应用本轮待提交补丁后，全量单元测试日志同样确认 `Ran 981 tests in 47.258s`、`OK (skipped=26)`。
- `python repository_guard.py`：通过，检查仓库候选文件的大文件、受保护目录和已知密钥模式。
- `docker compose -f deploy/local-llm/docker-compose.yml --profile gpu config --quiet`：通过，仅验证配置解析，没有启动 GPU 服务。
- `git diff --check`：通过。
- 部署证据 JSON 均可解析；15 条历史记录的原文、译文及脚本快照哈希一致，4 条当前原始响应的文件内容和 SHA256 一致。
- 独立只读代码审查覆盖翻译、检索、检查点及其已修改测试，未发现可复现的阻断回归；部署、文档、新增文件由主代理审查。55 个证据文件在 Git 暂存区与本机原文件逐字节一致。

历史 JSON 中的测试计数和服务状态是当时快照，不是本轮重新采集。当前 v13 提示词的真实译文、实际全库检索质量、GPU 稳定性与整书出版验收尚不能由这些程序测试证明；本轮没有启动模型、调用云端模型或替用户签署书稿验收。
