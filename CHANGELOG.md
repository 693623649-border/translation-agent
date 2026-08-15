# Changelog

本项目使用语义化版本。公共 RunSpec、Semantic IR、Artifact 和 Event schema 另行
版本化；应用版本升级不会自动改变既有数据 schema。

## Unreleased

- 增加 CLI/WebUI 共用的 `RunExecutionService`，统一 RunSpec 能力、targets、计划和执行。
- EPUB adapter 计划显式区分 Graph 与 adapter 步骤，拒绝尚无原生验证器的 report 目标。
- RunSpec JSON 改为严格类型校验，不再把字符串或整数隐式解释为布尔值。
- SQLite job registry 增加顺序 migration 与 worker identity；日志直接脱敏显式凭证。
- Web 取消改为租约保护的两阶段状态，实际 targets/profile 持久化用于产物判定。
- Web 计划与 worker 显式禁用仓库 `.env` 自动加载，保留可信 CLI 的兼容默认行为。
- semantic apply 在事务提交前拒绝输出树中的 symlink 和非 regular 目标。

## 0.1.0 — 2026-08-14

- 增加 `translation-agent` 统一产品 CLI、环境 doctor 和可安装 wheel。
- 增加版本化 RunSpec、Semantic IR、ArtifactRecord 与 RunEvent。
- 加固 DAG 的声明输入、深度隔离、缓存指纹与 draft/reader audit 边界。
- EPUB/PDF semantic apply 改为共享 QA、上游审计绑定和事务回滚。
- 增加 local-only Streamlit 多页工作台、SQLite WAL 任务、取消/恢复和正式产物目录。
- 增加仓库大文件/密钥 guard 与 Python 3.11/3.12 CI。

已知边界：EPUB-native release verifier 尚未接入，EPUB 输出只能作为草稿。
