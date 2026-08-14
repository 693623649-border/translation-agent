# Changelog

本项目使用语义化版本。公共 RunSpec、Semantic IR、Artifact 和 Event schema 另行
版本化；应用版本升级不会自动改变既有数据 schema。

## 0.1.0 — 2026-08-14

- 增加 `translation-agent` 统一产品 CLI、环境 doctor 和可安装 wheel。
- 增加版本化 RunSpec、Semantic IR、ArtifactRecord 与 RunEvent。
- 加固 DAG 的声明输入、深度隔离、缓存指纹与 draft/reader audit 边界。
- EPUB/PDF semantic apply 改为共享 QA、上游审计绑定和事务回滚。
- 增加 local-only Streamlit 多页工作台、SQLite WAL 任务、取消/恢复和正式产物目录。
- 增加仓库大文件/密钥 guard 与 Python 3.11/3.12 CI。

已知边界：EPUB-native release verifier 尚未接入，EPUB 输出只能作为草稿。
