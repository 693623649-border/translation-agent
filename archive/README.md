# 归档脚本

这里的文件仅供追溯历史运行方式，不是当前影印书编译框架的入口，也不保证适用于新的检查点格式。

- `karatani/` 下的三个脚本硬编码了《柄谷行人文学论集》的路径、章节标题和文本清洗规则。对应输出已经不在仓库中，请勿把它们当作通用排版或修复工具。
- `states_of_injury/` 下的两个脚本（`translate_epub.py`、`build_docx.py`）硬编码了《States of Injury》的绝对路径、章节顺序、标题译名与 calibre CSS 类，并直接读取 `DEEPSEEK_API_KEY`、绕过 profile/凭证体系。其 EPUB 解析、翻译、DOCX 排版功能已由 `epub_semantic_import.py` + `semantic_translation_runner.py` + `book_pipeline.build_docx` 的通用流程取代，请勿当作通用 EPUB 翻译入口。最终产物保留在 `book/States of Injury 中文版.docx`，术语表见 `work/states_of_injury_glossary.json`（可喂给正规流程的 `--glossary`）。
- `legacy/monitor.py` 面向旧版 `pdf_text_agent.py` 的 Windows 运行流程，依赖 Windows 进程探测命令和旧 `_checkpoints` 目录。在 Linux 和当前 `pages/page_XXXX.json` 流程中不能可靠判断任务是否存活。

当前主入口是仓库根目录的 `book_pipeline.py`。需要处理新格式检查点的人工辅助任务时，仍使用根目录的 `patch_translations.py` 和 `extract_textbook_layer.py`。
