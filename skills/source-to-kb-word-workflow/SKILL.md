---
name: source-to-kb-word-workflow
description: 把一个源文件（Markdown/纯文本/DOCX/EPUB/带文字层的 PDF）加入本地知识库，并产出逐字校对通过的中文 Word。当用户要求"把这本书/这篇文章加进知识库"、"产出 Word 版"、"和原文校对有没有缺字漏字"、"检查 Word 有没有不正常换行"、"入库并出中文稿"时使用；不用于扫描件 OCR、不用于翻译未译外文，也不用于修复已经出现的成品 Word 排版问题。
---

# 源文件入库与中文 Word 保真交付

这个工作流只有一个主张：**入库的语料和交付的 Word 都必须逐字等于源文件，而且这件事要被证明，不是被相信。**

因此禁止两条捷径：不要为了通过门去改 Word 或放宽阈值；不要手工编辑
`knowledge_base.jsonl`。任何差异都要回到源文件或发布器上解决。

配套插件 `dsh-kb-ingest` 提供三个工具（`kb_ingest_source` / `kb_verify_word` /
`kb_ingest_status`），它们是本 skill 的执行入口。开始前阅读
[保真门判读](references/fidelity-gates.md)，确定哪些结果必须停下来问用户。

## 工作流

1. **确认题目身份。** 书名决定每行的 `[书名]` 标题前缀、`book_id` 与 Word 封面，
   事后改名等于整库重建。用户没给就先用 `kb_ingest_status` 看是否已有同名工作区，
   仍然不确定就直接问。作者同理，会写进路由侧表并印在封面标题下。

2. **确认适配器能处理这个源。** 后缀决定解析方式与风险，见
   [源适配器](references/source-adapters.md)。扫描版 PDF 会被拒绝并给出 OCR
   主流水线命令。竖排日语扫描书改用 `japanese-vertical-kb` skill 与 `dsh-jp-vertical-kb` 插件，
   先完成有源图依据的 OCR、目录和译文核验，不要在文本适配器里硬凑。
   中文横排扫描书需要正文/注释分离与原书分段时，使用 `chinese-pdf-body-kb` skill 和
   `dsh-chinese-pdf-kb` 插件；清理后的 Markdown 才能作为本入口的源文件。
   英文原文 PDF 需要翻译为中文并区分选文、编者导言与脚注时，使用 `english-pdf-kb` skill
   和 `dsh-english-pdf-kb` 插件，先审阅目录角色和原页边界，再发布五字段语料与 Word。

3. **入库并出 Word。** 调用 `kb_ingest_source`，传 `source` / `title` / `author` /
   `output_dir`。工具会依次：解析源 → 建五字段语料与路由侧表 → 跑中文语言门 →
   用 `book_pipeline.build_docx` 出 Word → 逐字校对。

4. **按三种结果分别处理**（这是本 skill 最容易被跳过的一步）：
   - `blocked`：语言门拦下，**知识库没有写盘**。列出待译块数与语言，先翻译再重跑，
     或拿到用户明确同意后才用 `allow_foreign`。
   - `failed`：产物在盘上但不合格。读 `issues` 与 `hunks`，回到源文件修，或如实
     把 diff 报告给用户。不要试图"修 Word"。
   - `passed`：可以交付。仍然要把 warnings 念给用户听。

5. **交付前复核。** 用 `kb_verify_word` 独立重跑一次门（它会读回 `chapters.json`
   里记录的已发布文本，因此是真正的往返校验，不是复用上次结论）。用
   `kb_ingest_status` 报出语料规模、侧表覆盖率与两道门状态。

   涉及**扫描件 PDF 或页面级翻译**的书，还要确认 `translation.ellipsis` 检查通过：
   模型读不懂 OCR 时会用省略号把整句吞掉，这是**内容缺失**，而它在结构门、渲染门、
   中文门里全都看不见。判据与处置见 [译文省略号缺陷](references/ellipsis-defect.md)。

6. **报告。** 精确给出工作区、知识库文件、Word 文件、章节数、块数、缺字/多字/相似度、
   逐处 diff 与 warnings 的人工判断。跨书检索前提醒用户跑
   `python global_knowledge_base.py sync`。

## 绝不做的四件事

- **不编造内容。** 门只证明"Word 等于源文件"，不证明"源文件等于原书"。若源文件
  本身就是模型生成的，要在交付里明说这一层不确定性。同理，**不要全局删除 `……`**：
  原文自带的省略号（对话、目录引导点、欲言又止）必须原样保留，只能按"原文没有而
  译文有"逐页定位并重译。
- **不为过门而削足适履。** 缺字就补源文件，多字就删源文件里的多余内容；不要改
  Word、不要改阈值、不要改源文件去迁就产物。
- **不把 `allow_foreign` 当默认。** 它是唯一显式逃生口，只在用户明确同意时使用，
  并在交付里披露。
- **不用通用 Markdown 工具替代发布器。** 版面（A4、SimSun、Heading 1 分页、
  页码页脚、真脚注）全部由 `book_pipeline.build_docx` 决定；换渲染器等于换交付契约。

## 环境

插件默认按 `<仓库>/.venv` → `python` 的顺序找解释器，可用插件配置 `python` 或环境变量
`DSH_KB_PYTHON` 覆盖。请求一律通过 stdin 的 `--stdin-json` 传递，并在子进程里强制
`PYTHONIOENCODING=utf-8`：中文书名与路径若走 argv 或按控制台代码页解码，会在 Windows
上被破坏成代理字符并触发 `UnicodeEncodeError`。这两点已在插件里处理，不要改回 argv。

## 验证

改动本 skill 或 `tools/kb_ingest_plugin/` 后运行：

```bash
python -m unittest tests.test_kb_ingest
python -m unittest tests.test_project_skills
# 插件测试必须从 DSH profile 目录运行，否则 `@deepseek-ai/dsh-tools` 无法解析
(cd "$HOME/.dsh/profiles/desktop" && node "E:/Deeplearning/translation-agent/tools/kb_ingest_plugin/test/plugin.test.mjs")
```
