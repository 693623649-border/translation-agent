# macOS 独立分支维护

- 本工作目录的开发分支是 `macos`，用于本机原生 CPU OCR、文档产线和知识库维护。
- 保留当前 macOS 版本及已有历史；从分支拆分后独立发展。
- 不从 `origin/master`、`origin/deploy/*` 或其他 Windows/服务器分支执行 pull、merge、rebase，不自动向它们回流 macOS 改动。
- 可以执行 `git fetch origin` 和只读比较。跨分支移植必须由用户明确指定修复及目标分支；独立移植、检查后提交。
- `macos` 当前没有 upstream；只有用户明确要求发布时才 push，未来只能跟踪专用的远端 macOS 分支。
- 保留其他 worktree 的未提交工作。书籍、SQLite、模型、凭证和本地配置单独维护，不提交这些数据。
- 分支分工和运行方式见 [维护文档](docs/branches-and-local-knowledge-base.md)。
