# kb-reader：知识库哲学/文学问答插件

一个 DSH 宿主插件（bundle），把本仓库的书库变成会话里的三个工具：**检索取证 →
引文核验 → 再作答**。装进 profile 后，任何哲学、文学、思想史、文艺理论问题
都必须先查库再回答，答案里的每条书内论断都要标明《书名》·章节出处，每句
引用都能回到原文。

```
tools/kb_qa_plugin/
├── index.js            # DSH 插件：注册 kb_ask / kb_verify_quote / kb_library + 系统提示契约
├── kb_qa.py            # 检索核心：stdout-clean 的 JSON 网关（可独立当 CLI 用）
├── package.json        # dsh.bundle 清单
├── cordis.patch.yml    # bundle 层：插入 kb-reader 行
├── test/
│   ├── plugin.test.mjs     # 真语料 + 假 ctx，验证工具注册、schema、检索与核验
│   └── encoding.test.py    # 字节级 stdin/stdout 契约（GBK 控制台下中文不乱码）
└── README.md
```

配套工作流文档：[.dsh/skills/knowledge-base-close-reading/SKILL.md](../../.dsh/skills/knowledge-base-close-reading/SKILL.md)

## 三个工具

| 工具 | 用途 |
|---|---|
| `kb_ask` | 两段式检索：先在全库发现相关著作，再打开候选书做精读，返回带书名与章节定位的原文段落 |
| `kb_verify_quote` | 核验一句直接引用是否**逐字**存在：`verbatim` / `verbatim_normalized` / `mismatch` / `not_found` |
| `kb_library` | 列出工作区、块数与发布验收状态，确认库里到底有什么 |

插件本身不做检索排序。检索由 `kb_qa.py` 转调仓库里已经过测试的两层实现：
`global_knowledge_base.py`（仓库级 SQLite + FTS5，含装置降权与每书上限）与
`rag_knowledge_base.py`（单书 BM25 + 向量 RRF）。在 JavaScript 里重写排序会
让检索契约分叉，并与已发布的 `evaluate` 基线悄悄脱节，所以插件刻意只做一层
薄而可取消的适配。

## 安装

```bash
dsh plugin --profile <name> add <本目录绝对路径>
```

或从会话内用 `plugin_manager` 的 `install_bundle` 指向本目录。安装后
profile 的 `dsh.profile.bundles` 会追加 `dsh-kb-reader`，行本身由本目录的
`cordis.patch.yml` 插入。

### 依赖：项目下需要一条解析链接

插件 `import '@deepseek-ai/dsh-tools'` 来注册工具。DSH 把运行时包以
junction 形式挂在 `$DSH_HOME/profiles/node_modules`，而 Node 解析模块时会把
junction **还原成真实路径**（这些真实路径指向 deepseek-harness 检出），因此从
本仓库出发的常规向上查找找不到它们。补一条链接即可：

```powershell
New-Item -ItemType Directory -Force -Path node_modules | Out-Null
cmd /c mklink /J "<本仓库>\node_modules\@deepseek-ai" `
  "$env:LOCALAPPDATA\..\.dsh\profiles\node_modules\@deepseek-ai"
```

真实安装中 `$DSH_HOME` 通常是 `C:\Users\<你>\.dsh`。该目录已在 `.gitignore`
中忽略。插件在解析不到时会退回 `$DSH_HOME/profiles/node_modules`，两者都失败
才报错，并给出可操作的提示。

## 配置

在 profile 的 `cordis.patch.yml` 里按 `id: kb-reader` 覆盖（后层替换整份
config，需要保留的键要一并重述）：

| 键 | 默认 | 含义 |
|---|---|---|
| `python` | 探测 `<repo>/.venv` → `python` | 运行 `kb_qa.py` 的解释器；未设时可用 `DSH_KB_PYTHON` |
| `askTimeoutMs` | `180000` | `kb_ask` 单次上限；精读阶段可能联网做 embedding |
| `quoteTimeoutMs` | `60000` | `kb_verify_quote` 单次上限 |
| `libraryTimeoutMs` | `30000` | `kb_library` 单次上限 |
| `defaultHits` | `6` | 未传 `limit` 时的段落数 |
| `defaultMaxChars` | `28000` | 未传 `max_chars` 时的总字符预算 |
| `shelfLimit` | `80` | `kb_library` 默认条数 |
| `contract` | `true` | 是否注入"知识库优先"系统提示契约。契约要求：阅读任务先调用 kb_ask 在 RAG 侧知识库取证、再作答；回答中每条书内论断标明《书名》·章节出处 |

环境变量：`DSH_KB_PYTHON`、`DSH_KB_REPO_ROOT`、`DSH_KB_GLOBAL_DB`、
`DSH_KB_OUTPUTS_ROOT`、`DSH_KB_SCRIPT`。

## 正式取证模式

`kb_ask` 固定 `hybrid`（BM25 + 向量召回 + RRF）、`scope=reader`、开启精读。
SQLite FTS 只发现候选书；最终 `hits` / `context` 仅使用成功 hybrid 的精读段落，
并继续经过证据门、去重与字符预算。缺向量、provider 或 embedding 失败不会静默
退回 lexical；全部候选失败报告 `hybrid unavailable`。部分候选失败则返回成功
取证结果并标明 `partial_coverage` 和 `skipped`。正常 hybrid 执行但证据门未通过
返回空证据，不能等同于索引不可用。

Python/CLI 的 `semantic=False`、`--lexical`、`--no-deep` 仅供显式离线诊断，
会标明 `diagnostic_only`、`requested_mode` 和 `effective_mode`；宿主正式工具不提供
这些选项。pages/archive/all 只能用于低层发现诊断和引文查验，不能声称有 reader
之外的 hybrid 覆盖。引文仍须用 `kb_verify_quote` 逐字核验。

## 直接当 CLI 用

脱离会话时同一套检索可单独运行，输出一个 JSON 文档：

```bash
python tools/kb_qa_plugin/kb_qa.py status --limit 20
python tools/kb_qa_plugin/kb_qa.py ask "交换样式" --mode hybrid --limit 5
python tools/kb_qa_plugin/kb_qa.py ask "国民性" --workspace "知识库_鲁迅全集" --lexical
python tools/kb_qa_plugin/kb_qa.py ask "校正後" --scope pages --no-deep
python tools/kb_qa_plugin/kb_qa.py verify-quote "从来如此，便对么？"
```

`ask` 的参数：`--workspace`、`--scope reader|pages|archive|all`、`--limit`、
`--no-deep`、`--per-book-cap`、`--max-chars`、`--hit-chars`、`--verified-only`、
`--mode hybrid`、`--lexical`、`--apparatus-weight`、`--deep-books`。

宿主插件通过 `--stdin-json` 传请求：

```bash
echo '{"command":"ask","args":{"query":"交换样式","limit":3}}' | python tools/kb_qa_plugin/kb_qa.py --stdin-json
```

## 三个刻意的设计决定

**证据门。** 向量检索没有相关度下限：问一个语料里根本不存在的词，它照样返回
最近的邻居，而那些段落看起来和真证据一模一样。所以精读阶段的命中只有在
"落在发现阶段已经匹配到的章节"、"查询已按书名/作者名路由到该书"或"段落里真的
出现查询词元"时才被采纳。没有这道门，一个语料回答不了的问题会带着像模像样的
引用回来——这是引用优先的读者最不能有的失败模式。被拦下的 ID 与书名记在
`diagnostics.deep_rejected_ids` / `deep_rejected_books`。

**书目通道（按名路由）。** 跨书发现只匹配块**正文**。于是中文书名是外文短语的书
（`日本现代文学的起源`、`反文学論`）只能靠它的中文正文被找到，而正文本身是外文的
书**根本够不着**——书名不在任何块的正文里，FTS5 trigram 没有东西可匹配。所以查询
里写出的书名或作者名会被**直接**读成路由证据（`route_by_name`），把该书送进精读
阶段，而不是让它去和正文 trigram 竞争。匹配前会先按标点切分工作区目录名并剔除
打包噪声（`z-library`、`1lib`、`汉译世界学术名著丛书`），否则每个查询都会路由到全库。
两字符的短名（作者姓氏）只在**整条查询就是它**时才算路由，避免"鲁迅 国民性"这种
概念提问把全集拉进来。

**大小写不折叠。** 核验只归一空白与全角标点，不折叠大小写。语料印的是
`A.赠与的互酬`，你写 `a.赠与的互酬` 就会被报成 `verbatim_normalized` 并附上
逐字符差异；`verbatim` 才允许照引。把大小写折掉会让一个读者在页面上看得见的
差别伪装成逐字一致——正是核验要防的那种假通过。同一理由，返回的 `excerpt`
一律切自**原文**，不返回折叠后的视图。

**请求走 stdin，不走 argv。** Windows 上 argv 会经过控制台代码页再编码，中文
查询会以乱码抵达并静默返回空结果。`kb_qa.py` 同样显式以 UTF-8 读取 stdin、
写 stdout（见 `force_utf8_stdio`），否则 GBK 控制台下第一个日文中黑点就会让
整个 JSON 文档写不出去。

**空结果必须给方向。** 静默返回空是这套工具最贵的失败：语料里有材料，只是查询
够不着，而调用方读到的却是"库里没有"。所以 `diagnostics.hints` 会在空结果时写明
下一步——该按名路由、该换原文语言、还是该把书名写全。

## 测试

```bash
node tools/kb_qa_plugin/test/plugin.test.mjs            # 真语料 + 假 ctx
python tools/kb_qa_plugin/test/encoding.test.py         # 字节级编码契约
python -m unittest tests.test_global_knowledge_base     # 检索层回归
python -m unittest tests.test_kb_translation            # 翻译层回归

# 语料维护（不依赖会话）
python tools/kb_qa_plugin/test/foreign_census.py        # 还有多少块需要翻译
python tools/kb_qa_plugin/test/translate_all.py         # 批量翻译（可中断续跑）
python tools/kb_qa_plugin/test/list_remaining_foreign.py # 剩余命中语言门的块及其性质
python tools/kb_qa_plugin/test/verify_translation.py    # 翻译后语料完整性 + 索引状态
python tools/kb_qa_plugin/test/translation_quality.py   # 译文碎裂度（对照源质量）
python tools/kb_qa_plugin/test/reregister_translated.py # 重建被翻译作废的向量索引
```

语料里的中文夹日文术语残留在 `list_remaining_foreign.py` 里按"外文正文"与
"中文夹日文术语"两类分开报，因为两者的处置完全相反：前者要翻译，后者**不该**
再翻译（再译只会把已经正确的中文改坏），只能带 `--allow-foreign` 注册。

`plugin.test.mjs` 直接跑真实索引，所以它能挡住 Python JSON 契约、工具 schema 与
注册接线的漂移。该测试可能请求真实 embedding API；日常策略验证使用离线测试 `python -m unittest tests.test_kb_qa_hybrid_policy`。

## 已知限制

- **`kb_ask` 的回答仍需人工判断**：证据门只保证"这些段落确实谈到查询词"，
  不保证它们就是问题的答案；模型仍可能误读。
- **单书精读最多两本**（`deep_books`），跨书比较需要分次限定工作区检索。
- **繁简转换不做**：检索用库内统一字形命中，但引文核验按原字形严格比对；
  跨字形引用会报 `mismatch`，这是刻意的。
- **验收状态不阻断检索**：`report_status=missing` 的工作区照常可检索，只是
  答案里会显示"未验收"。
