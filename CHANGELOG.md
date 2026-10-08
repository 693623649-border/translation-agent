# Changelog

本项目使用语义化版本。公共 RunSpec、Semantic IR、Artifact 和 Event schema 另行
版本化；应用版本升级不会自动改变既有数据 schema。

## 未发布

- 新增四个 DSH/Cordis 入库插件与配套 skill：源文件入库与中文 Word 保真交付
  （`dsh-kb-ingest`）、竖排日语扫描书（`dsh-jp-vertical-kb`）、中文横排扫描
  正文重建（`dsh-chinese-pdf-kb`）、英文选文译中（`dsh-english-pdf-kb`）；另有
  `kb-reader` 问答插件（检索取证 → 引文核验 → 再作答）。正式调用规则见
  `docs/knowledge-base-call-policy.md`，全局库两条链路与存储设计见
  `docs/global-knowledge-base-dataflow.md`。
- Python 高层 API 与 `kb_ask` 默认真实 hybrid：向量缺失、embedding 身份不一致
  或 provider 失败时显式报错，禁止纯 BM25 静默降级冒充 hybrid；新增离线策略
  回归测试（不调用 embedding 服务）。
- 修正扫描书逐页翻译配置：DeepSeek Flash 思考模式可能返回空正文，但旧流水线仍会标记页已完成。默认改为请求可见译文，并拒绝空译文、残留外文段落和原文没有的省略号；复杂页仅对失败段落做定点补译。括号内的短注音或原名（如「基里尔（キイ）」）视为正当译注，不触发外文残留修复。
- 竖排 OCR 改为按页检测方向；日文纵排按右到左的列序和上到下的行序排列，并保存原始行框与排序版本。目录按源书页码核对，日文原题另作别名，用来剥除重复的篇首页眉。
- 新增中文语言质量门，覆盖数据库与 Word 输出：发布校验器新增
  `knowledge_base.chinese` 与 `docx.chinese` 检查（复用 kb_translation /
  docx_translation 的离线判定，索引/对照表/书目等双语装置豁免）；
  `global-knowledge_base sync` 默认拒绝未翻译外文 reader 块并列出工作区清单，
  `--allow-foreign` 临时绕过并在 meta 记录 `chinese_gate`。整体管线图与门说明
  见 `docs/product-architecture.md`。
- `translation-agent-kb translate-kb` 新增 `--concurrency`，便于在 Provider
  限速（如 Zhipu 1302）时降速重跑；翻译失败仍保持 fail-closed，不写入语料。
- 全局知识库 schema 升至 v3：`verify` 除正文源文件与资产外，同时跟踪装置旁车、
  发布报告及 `toc.json`/`semantic-review.json`/`review-decisions.jsonl` 的哈希，
  同步后被改动即报告过期；`report_status` 明确为同步时快照。
- 装置旁车存在但损坏、`documents_sha256` 过期或标注未覆盖全部行时，`sync`
  显式失败并保留旧库，不再静默把降权重置为 1.0。
- 短 CJK 词检索修复：一两个汉字的词不再被三元组切词丢弃；纯短词查询按
  "全部出现"组合匹配（原为永不命中的整串回退），混合查询中短词只用于
  摘要高亮；子串回退分支同样应用装置降权。
- 每书限额改在轻量候选行（不含正文）上计算后再取回正文，单一书籍的高频
  命中不再把其他书挤出取数截断。
- 同步前记录每个已解析/哈希源的 mtime 与大小，替换前复核，期间被改动的
  源会中止同步；`chapters.json` 改为单次读取同时供解析与哈希。
- `evaluate` 的工作区数量校验从等值改为下限，新增书籍不再使检索质量门失败。
- 修正中文语言门的假阴性：OCR 残留的日文引注（店名、图版碎片、书名）让已是中文的
  reader 块被 `detect_language` 判为 `ja`，而重译对这类块是空操作（模型原样返回），
  语言门因此永久卡住。`kb_translation.classify_row` 新增汉字主导判据——假名占比低于
  20 % 且汉字足够时按 `chinese_with_quote_residue` 豁免；阈值取自实测分布：竖排日文
  源页的假名占比从未低于 0.52（202 页样本），最密的日文版权页为 0.239，实测误报块最高
  0.162。`tests/test_kb_translation.py` 为三类边界（引注残留、日文版权页、英文夹汉字）
  加了锁定用例，「参考作品」并入书目豁免标记。
- 竖排入库插件的产物摘要不再计入 Office 锁文件（`~$*.docx`）：打开成品不再使已绑定的
  验收/注册凭据失效。

## 0.1.0 — 2026-08-14

- 增加 `translation-agent` 统一产品 CLI、环境 doctor 和可安装 wheel。
- 增加版本化 RunSpec、Semantic IR、ArtifactRecord 与 RunEvent。
- 加固 DAG 的声明输入、深度隔离、缓存指纹与 draft/reader audit 边界。
- EPUB/PDF semantic apply 改为共享 QA、上游审计绑定和事务回滚。
- 增加 local-only Streamlit 多页工作台、SQLite WAL 任务、取消/恢复和正式产物目录。
- 增加仓库大文件/密钥 guard 与 Python 3.11/3.12 CI。

已知边界：EPUB-native release verifier 尚未接入，EPUB 输出只能作为草稿。
