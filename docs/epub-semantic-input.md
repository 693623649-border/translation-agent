# EPUB 文档语义层输入

`epub_semantic_import.py` 为 born-digital EPUB 提供确定性输入层，不运行
OCR，也不把 EPUB 的物理文件拆分规则混入 PDF pipeline。它按 OPF spine
读取正文，将 XHTML 转为仓库现有发布器可接受的 `chapters.json` 和章节
Markdown，并建立 EPUB `noteref` 与脚注/尾注定义的稳定关系。

## 1. 导入 EPUB

```bash
python epub_semantic_import.py import "book.epub" -o "work/book-semantic"
```

主要产物：

- `chapters.json` 与 `chapters/*.md`：可直接交给现有 EPUB/DOCX 发布函数；
- `semantic/source_chapters/*.md`：不可变的英文语义基准；
- `semantic/translation-units.jsonl`：细粒度、带源文本 SHA-256 的翻译单元；
- `audit/semantic-reconstruction.json`：spine、脚注闭环和阻断项审计。

匿名脚注元素只在紧跟某个带 ID 脚注时作为续段合并；孤儿匿名脚注阻断
发布。索引和页表中的 printed-page locator 只保留可见页码文本，不把原
EPUB 文件坐标泄漏到新出版物。

Python API：

```python
from pathlib import Path
from epub_semantic_import import import_epub

report = import_epub(Path("book.epub"), Path("work/book-semantic"))
if report["release_blocked"]:
    raise RuntimeError(report["audit"])
```

## 2. 准备或运行翻译

`prepare` 只生成模型提示，不联网；可先检查所有 protected markers：

```bash
python semantic_translation_runner.py prepare \
  work/book-semantic/semantic/translation-units.jsonl \
  -o work/book-semantic/semantic/prepared.jsonl \
  --glossary glossary.json \
  --config pipeline.example.toml \
  --translation-profile deepseek_flash
```

显式使用 DeepSeek 时才运行 `run`。API key 只从环境变量读取；缓存键包含
源文本、模型、prompt contract、目标语言和术语表：

```bash
DEEPSEEK_API_KEY=... python semantic_translation_runner.py run \
  work/book-semantic/semantic/translation-units.jsonl \
  -o work/book-semantic/semantic/translations.jsonl \
  --glossary glossary.json \
  --config pipeline.example.toml \
  --translation-profile deepseek_flash \
  --max-chars 9000 \
  --concurrency 16 \
  --retries 3 \
  --cache-dir work/book-semantic/.translation-cache
```

脚注引用、脚注定义标记和内联结构会被替换成顺序敏感的占位符。模型少
传、多传或重排占位符时，本步骤失败，不写入可发布译文。runner 按章节
内相邻单元组成批次，每个单元还有不可变的 `UNIT` 边界；批次以独立哈希
缓存并并发执行，响应会检查边界顺序、结构 token、合理长度及中文覆盖。
只有通过全部验证的完整运行才会原子写入翻译 JSONL。

## 3. 回填并发布

```bash
python epub_semantic_import.py apply-translations \
  -o work/book-semantic \
  work/book-semantic/semantic/translations.jsonl

python book_pipeline.py -o work/book-semantic --phase epub \
  --title "译名" --target-language 简体中文

python book_pipeline.py -o work/book-semantic --phase docx \
  --title "译名" --author "作者"
```

回填会再次验证翻译单元集合、源 SHA-256、引用顺序、定义 ID 和一对一脚
注闭环；任何结构漂移都会阻断。若需要正式发布，还应按现有
`publication_verifier` 契约运行对应 EPUB/DOCX 验证门。
