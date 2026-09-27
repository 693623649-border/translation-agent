# 分支分工与 macOS 知识库维护

## 维护选择

在 Mac 上维护现有书籍的跨书知识库、更新 SQLite 索引并调用本地检索，
持续使用 `master`。原生 CPU PaddleOCR、新书生产、DOCX 派生知识库及
向量/混合检索仍使用 `codex/native-paddleocr-macos`，这些能力尚未整体回流主干。
如果当前工作主要是从扫描书持续生产语料并做混合 RAG，应在 macOS 分支开发，
将通用功能经过测试后回流 master。仅因运行在 Mac 上，无需切换分支。

## 各分支用途

以下以 2026-09-27 的 fetch 结果和本次 master 集成为依据；提交数量会继续变化。

| 分支 | 主要用途 | 维护建议 |
| --- | --- | --- |
| `master` | 产品架构主干：统一执行、EPUB DAG、发布门、哈希绑定人工评审；本次加入修复版跨书 SQLite/FTS5 知识库 | 跨书知识库和通用架构的长期维护入口 |
| `codex/native-paddleocr-macos` | 原生 CPU PaddleOCR、书籍生产、DOCX 处理、RAG 注册、BM25/向量混合检索及重排 | macOS 生产能力继续在此开发，逐步回流通用改动 |
| `codex/global-knowledge-base` | 一个提交的早期跨书索引原型 | 已被本次 master 集成替代，不作为长期维护线 |
| `codex/debug-latest` | macOS 线最新提交之前的历史快照，没有相对 macOS 线的独有提交；上游却是 GPU 部署线 | 调试残留，当前保留，不继续开发 |
| `origin/deploy/paddleocr-rtx5090` | NVIDIA GPU/Docker 部署与服务器产线，以及知识库修复和书籍工具 | GPU 部署专用；本次只取知识库模块及相关测试，不合并整条部署线 |

集成前 master 与 macOS 线各有 5 / 31 个独有提交。两条线尚未合并；
不能把 macOS 线视为已经包含 master 的新发布门，也不能把 master 视为已经
包含原生 PaddleOCR 和混合 RAG。原来 5 个主干提交为：
`c2257aa`、`17c05c1`、`f8f620b`、`d094912`、`9ec7b3e`。

## 代码与书籍数据分开维护

本机核对到的路径：

- 主干代码：`/Users/dddkazusa/translation-agent`。
- macOS 产线代码：`/Users/dddkazusa/translation-agent-debug`（独立 worktree）。
- 现有书籍数据：`/Users/dddkazusa/translation-agent/outputs`。
- 跨书派生索引：`/Users/dddkazusa/translation-agent/global_knowledge_base.sqlite3`。

核对时 macOS worktree 下没有 `outputs/`，且有未提交的前端改动。
在该 worktree 运行产线时，显式传入主数据目录下的绝对输出路径。
同一本书避免两个任务同时写入。两个 worktree 应使用各自的虚拟环境，
避免 editable install 的命令指向另一条分支。

Git 提交只保存代码、测试和文档。`outputs/`、SQLite、原书和本地配置已被忽略，
不会随 `git push` 备份；书籍数据需要单独备份。更新书籍内容不要求 Git 提交。

## 更新与调用跨书知识库

在主干根目录、已安装项目依赖的环境中执行：

```bash
cd /Users/dddkazusa/translation-agent
.venv/bin/python global_knowledge_base.py \
  --db /Users/dddkazusa/translation-agent/global_knowledge_base.sqlite3 \
  sync --outputs /Users/dddkazusa/translation-agent/outputs
.venv/bin/python global_knowledge_base.py status
.venv/bin/python global_knowledge_base.py verify
.venv/bin/python global_knowledge_base.py search '自然主义' --limit 5
.venv/bin/python global_knowledge_base.py search '私小説' \
  --workspace 私小説論 --scope pages
```

每次新增书籍、修订正文或更新 apparatus 侧表后重跑 `sync`；它全量构建派生索引，
成功后原子替换数据库，不修改书籍内容。旧 schema v1 也通过 `sync` 升级。
已有 `knowledge_base.jsonl` 是主要检索语料；缺失时按 `chapters.json` 导入 Markdown。
单独放入 PDF/DOCX 不会自动 OCR、翻译或产生正文索引，应先用相应产线处理。

从任意工作目录调用已安装的入口时，显式指定数据库：

```bash
/Users/dddkazusa/translation-agent/.venv/bin/translation-agent-global-kb \
  --db /Users/dddkazusa/translation-agent/global_knowledge_base.sqlite3 \
  search '自然主义' --limit 5
```

在加载本仓库模块的 Python 环境里调用：

```python
from global_knowledge_base import search, sync_outputs

db = "/Users/dddkazusa/translation-agent/global_knowledge_base.sqlite3"
sync_outputs("/Users/dddkazusa/translation-agent/outputs", db)
hits = search("自然主义", db_path=db, limit=5)
```

此入口返回命中文段与来源，属于本地词法检索，不会自动调用大模型生成答案。
向量 embedding、混合检索和重排请使用 macOS 分支的 `translation-agent-kb`。
可将命中的 `content` 和来源元数据交给需要调用知识库的应用。

## 验收与回流

`verify` 检查已登记语料、资产及新文件是否与索引一致，不代表 OCR/译文已校对。
`--verified-only` 按索引保存的发布状态过滤；发布报告变化后也应重跑 `sync`。

```bash
.venv/bin/python global_knowledge_base.py evaluate \
  --cases tests/fixtures/global_kb_retrieval_cases.json
```

该题集绑定原有 18 个工作区及 35 道检索题，是语料快照验收，不是任意用户
目录都应通过的通用测试。增删书籍后需人工复核并更新题集，不能为过门而
降低阈值。日常先运行单元测试，再做自己语料的检索验收。

长期目标是将经过验证的通用 OCR/RAG 能力回流 master，让 GPU/macOS 差异由
配置与后端选择表达。本次不执行大规模分支合并或删除旧分支。
