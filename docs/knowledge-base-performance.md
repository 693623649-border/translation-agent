# 知识库运行内核与性能验证

2026-10-02 实现并验证。单书 JSONL、向量和装置注释仍是发布方拥有的
规范产物；全局 SQLite 是可重建的本地索引。此次没有改写原始语料。

## 已实现

- 单书打开时只解析一次正文、只加载一次 ready 向量。正文与向量的
  解析和 SHA-256 使用同一字节快照；manifest、模型身份、元数据和
  装置注释的验证契约保留。
- 每个库快照预计算规范化紧凑正文，查询复用它；BM25、完整查询的
  子串加分、装置权重、tie-break 和混合候选深度保持一致。
- 无需完整排序的 top-k 使用标准库 heapq；需要跨书限额的单书
  路径保留完整排序，防止候选提前截断改变结果。
- 全局检索在 SQLite 内物化 BM25 评分，再用窗口排名执行每书限额，
  最后取得 LIMIT 范围内的正文。SQL 内部仍可能排序，Python 不再
  fetchall 全部候选再排序。
- schema 4 的 contentless FTS5 存储中文、假名及韩文的一字/二字
  倒排，避免纯短词全文扫描；长词仍用原 trigram BM25。混合长短词
  查询保留原有召回规则。短词索引不保存第二份编码 token 文本。
- 同步对全部来源做严格 SHA 清单比较，仅重建变化工作区。完全
  未变时不解析章节、重建索引或写数据库。变更时通过 SQLite backup
  生成临时快照，完成来源稳定性、语言门及索引结构检查后原子替换。
  新增、删除、装置注释、报告依赖和资产更新均纳入变化检测。

## 命令与诊断

```powershell
# 自动迁移旧 schema，之后按工作区增量同步
python global_knowledge_base.py sync --outputs outputs

# 强制重建，用于恢复或比较
python global_knowledge_base.py sync --outputs outputs --full-rebuild

# 完整来源与数据库校验
python global_knowledge_base.py verify

# 本机已审查题集的质量门
python global_knowledge_base.py evaluate --cases tests/fixtures/global_kb_retrieval_cases.local.json
```

同步报告包含 `mode`、`updated_workspaces`、`reused_workspaces` 和
`deleted_workspaces`。`unchanged` 表示来源清单未变；它不代表刚执行了
完整数据库校验。数据库损坏检查使用 `verify`，恢复使用 `--full-rebuild`。

## 本机复测

以任务开始前的源码和 SQLite 快照为基线；每条查询取三次中位数。
查询不调用模型提供方。

| 场景 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 鲁迅全集，复用已打开对象，查询“社会 个人 文学” | 516.9 ms | 31.1 ms |
| 王小波，复用已打开对象，同一查询 | 190.9 ms | 17.3 ms |
| 全局“资本主义” | 50.8 ms | 37.2 ms |
| 全局“自然” | 164.2 ms | 48.4 ms |
| 全局“自然 正式” | 215.9 ms | 62.8 ms |

该表记录的是词法离线诊断基准，不是正式问答的模式选择；正式调用统一
hybrid，见 `docs/knowledge-base-call-policy.md`。

首次打开鲁迅全集仍需约 2.53 秒（基线 2.62 秒），因为完整校验、
向量解析和预计算仍发生在打开时。未引入跨 API 请求缓存或懒向量加载，
每次 open 继续验证所有输入。

现有 71 个工作区、19,635 块已迁移。未变同步复用全部 71 个工作区，
耗时约 3.46 秒；本次完整迁移约 92.9 秒。严格来源哈希仍随输入字节数
增长；变化同步的 backup 和完整性检查仍随数据库大小增长。

本机 46 题与基线逐项比较，ID、分数、顺序全部一致。跨书 Hit@1 为
86.7%、Hit@5 为 100%；章节 Hit@1 为 87.5%、Hit@5 为 100%。来源
校验 current=true，SQLite integrity=ok；301 个正文和侧车文件哈希保持
不变。该结果证明本机题集未回归，不代表所有查询或生产延迟分布。

短词索引占用额外磁盘：数据库从约 504.4 MiB 增至 541.3 MiB。
contentless 存储避免保存重复 token 文本；不宣称所有类型的内存或磁盘
使用都下降。

结果文件位于 `work/kb-core-optimization/benchmark.json` 和 `sync.json`。
复测工具为 `tools/benchmark_kb_core.py`，需要任务开始前保存的模块、
`baseline.sqlite3` 以及可选的 `corpus-hashes.json`。

```powershell
python tools/benchmark_kb_core.py --baseline <任务开始前的备份目录>
```

精确向量搜索仍为 O(ND)；SQLite 普通索引不会消除该计算成本。
