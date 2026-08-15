# Changelog

本项目使用语义化版本。公共 RunSpec、Semantic IR、Artifact 和 Event schema 另行
版本化；应用版本升级不会自动改变既有数据 schema。

## Unreleased

- 增加 CLI/WebUI 共用的 `RunExecutionService`，统一 RunSpec 能力、targets、计划和执行。
- EPUB 升级为 first-class DAG：源 ZIP 身份、spine 语义重建、单元翻译/回填、reader
  物化、EPUB/Word 发布均进入共享 Graph 的目标闭包、检查点和审计日志。
- 增加 `core.publication.verify.epub` 与 `publication.epub_report`：无模型核对源/语义
  哈希、脚注闭环、EPUB3 package/spine/nav、资源清单、内部链接和成品身份。
- CLI/WebUI 默认以 EPUB native report 为正式目标；关闭质量门时裸 EPUB/Word
  保持草稿，EPUB 来源的 Word 仍无独立正式发布 profile。
- EPUB 与 born-digital PDF writer 统一输出 canonical `TranslationUnit`（8 个固定字段
  和 `locators[]`）；runner/apply 对旧 `source_href`、`source_pages` 与 `kind=list`
  保留 schema-v1 兼容读取，当前 writer 不再产生旧形状。
- RunSpec JSON 改为严格类型校验，不再把字符串或整数隐式解释为布尔值。
- SQLite job registry 增加顺序 migration 与 worker identity；日志直接脱敏显式凭证。
- Web 取消改为租约保护的两阶段状态，实际 targets/profile 持久化用于产物判定。
- Web 计划与 worker 显式禁用仓库 `.env` 自动加载，保留可信 CLI 的兼容默认行为。
- semantic apply 在事务提交前拒绝输出树中的 symlink 和非 regular 目标。
- 增加 hash-bound 人工复核闭环：raw reconstruction audit 保持不可变，append-only
  决议、中央 policy、内容寻址 review audit、effective semantic bundle、Graph 节点、
  EPUB/Word/full verifier 与 Web 正式产物目录共同验证同一 provenance 链。
- 发布验收改由 legacy CLI 与 PDF DAG 共用的类型化 `publication_service` 调用；Graph
  verify 不再拼接 argv 或调用私有 `_main_unlocked`。首个确定性检查
  `runtime.hygiene` 已拆入 `publication_checks/`，报告 ID、schema 与检查顺序不变。
- 章节编译改由阶段式 CLI 与 PDF DAG 共用的类型化 `compile_service` 调用；
  `core.chapters.compile` 不再经 argv 或私有 `_main_unlocked` 回放阶段入口，并完整保留
  页集合、OCR 模型、目录映射、粒度和翻译身份校验。编译仅返回章节清单与知识库行，
  publisher 和 release verifier 仍由独立节点负责。

## 0.1.0 — 2026-08-14

- 增加 `translation-agent` 统一产品 CLI、环境 doctor 和可安装 wheel。
- 增加版本化 RunSpec、Semantic IR、ArtifactRecord 与 RunEvent。
- 加固 DAG 的声明输入、深度隔离、缓存指纹与 draft/reader audit 边界。
- EPUB/PDF semantic apply 改为共享 QA、上游审计绑定和事务回滚。
- 增加 local-only Streamlit 多页工作台、SQLite WAL 任务、取消/恢复和正式产物目录。
- 增加仓库大文件/密钥 guard 与 Python 3.11/3.12 CI。

当时的已知边界：0.1.0 尚未接入 EPUB-native release verifier，EPUB 输出只能
作为草稿；此边界已在 Unreleased 中解除。
