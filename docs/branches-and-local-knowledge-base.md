# 分支分工与 macOS 知识库维护

## 维护选择

现在日常开发和运行统一使用 `master`：Mac 原生 CPU OCR、新书生产、DOCX 处理、
单书/合辑 RAG 和跨书 SQLite/FTS5 索引均已合入。无需再为导入 PDF 切换分支。

## 各分支用途

| 分支 | 合并后的用途 | 维护建议 |
| --- | --- | --- |
| `master` | 原生 CPU OCR、PDF/EPUB/DOCX 生产、统一 DAG、发布门、RAG、跨书索引 | 日常唯一维护主线 |
| `codex/native-paddleocr-macos` | 本次合入的 macOS 产线历史，已提交内容包含在 master 中 | 保留作历史参照；原 worktree 尚有未提交前端工作，不删除 |
| `codex/global-knowledge-base` | 早期知识库原型 | 已被 master 中修复版替代 |
| `codex/debug-latest` | 过时调试快照 | 不继续开发 |
| `origin/deploy/paddleocr-rtx5090` | GPU 部署线，仍含独立的服务器工具与修复 | 部署专用，不整体合并；通用修复按需回流 |

本次合并保留 master 的统一执行服务、哈希绑定评审和原生 EPUB 发布门，
将原 macOS 分支已提交的 31 个提交纳入历史。原生 OCR 参数通过同一执行服务
传给 CLI/Web 后台；旧 EPUB 适配器的入库能力已迁入 DAG。OCR/编译/发布缓存
版本已更新，防止合并后误用旧产物。

## 从 PDF 添加新书

首次为主干环境安装依赖；本机已有模型无需重新下载：

```bash
cd /Users/dddkazusa/translation-agent
.venv/bin/python -m pip install -e '.[web,legacy,paddle]'
.venv/bin/translation-agent-paddle status --variant mobile
# 仅在 status 提示缺少模型时运行：
# .venv/bin/translation-agent-paddle setup --variant mobile
```

先用本机 CPU 做 OCR，不请求翻译或目录模型：

```bash
.venv/bin/translation-agent run /absolute/path/new-book.pdf \
  -o /Users/dddkazusa/translation-agent/outputs/new-book \
  --source-mode scanned-pdf --phase ocr --target pages.raw \
  --ocr-backend paddleocr-native --paddle-native-variant mobile \
  --no-translate --no-verify --no-rag-embed
```

这里的 `--no-verify` 仅用于原始 OCR 检查点，不能把它视为已验收知识库。
随后完成目录/章节组织、必要的翻译与校对，再运行完整生产流程：

```bash
.venv/bin/translation-agent run /absolute/path/new-book.pdf \
  -o /Users/dddkazusa/translation-agent/outputs/new-book \
  --source-mode scanned-pdf --ocr-backend paddleocr-native \
  --paddle-native-variant mobile --config pipeline.toml \
  --target publication.report --no-rag-embed
```

目录、校勘和翻译阶段仍使用所选 Profile 的文本模型；本机 OCR 不等于整条流程离线。
原文无需翻译时加 `--no-translate`。有文字层的 PDF 使用 `--source-mode text-pdf`。
如已有人工目录，可用 `book_pipeline.py --phase toc --toc-json ... --page-offset ...`
导入，避免模型识别目录。完整报告保留 OCR 人工抽样、语义评审、Word 渲染等门槛；
按报告修复后重跑，不能把 `--no-verify` 作为正式发布的替代。

未审定 OCR 正文进入正式知识库前，需要 `audit/ocr-quality-samples.json` 中的
源 PDF SHA-256 与人工核对样本；格式和数量见
[发布质量门](../skills/pdf-translation-pipeline/references/release-gate.md)。
正文修改后重建书籍产物，确认验收状态，再运行下文的跨书 `sync`。

需要网页界面时，使用同一环境的 `.venv/bin/translation-agent-web`，
在“扫描 PDF”中选择“本机 PaddleOCR（CPU）”。网页任务使用独立任务目录；
跨书索引默认扫描 `outputs/`，需将选定成品工作区复制至该目录后同步，
或显式用 `sync --outputs` 指定实际成品父目录。

## 代码与书籍数据分开维护

本机核对到的路径：

- 主干代码：`/Users/dddkazusa/translation-agent`。
- macOS 产线代码：`/Users/dddkazusa/translation-agent-debug`（独立 worktree）。
- 现有书籍数据：`/Users/dddkazusa/translation-agent/outputs`。
- 跨书派生索引：`/Users/dddkazusa/translation-agent/global_knowledge_base.sqlite3`。

合并后在主干工作目录运行生产与检索即可。原 macOS worktree 有尚未提交的
前端开发，已原样保留；本次仅合入其已提交内容。同一本书避免多个任务同时写入。
各 worktree 保留独立虚拟环境，避免 editable install 的命令指向另一条分支。

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
向量 embedding、混合检索和重排现在也使用主干环境的 `translation-agent-kb`。
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

后续通用 OCR、RAG 与知识库改动均从 master 开发并回流 master。GPU/macOS
通过后端与配置区分；历史分支不自动删除，以保留未完成工作。
