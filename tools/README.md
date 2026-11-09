# tools/ 工具清单

本目录收录流水线主入口之外的辅助工具。每个工具的定位与维护状态如下。

## 流水线级工具

| 文件 | 用途 | 维护状态 |
|---|---|---|
| `local_paddleocr_import.py` | 渲染 PDF → Docker GPU PaddleOCR 分片识别 → 导入 PageRecord。支持 `--start-page/--end-page` 页范围，作为 `book_pipeline --ocr-backend paddleocr-local` 的执行体 | **活跃**（book_pipeline 内建后端依赖它） |
| `magpie_ocr.py` | 渲染 PDF → Magpie（OpenAI 兼容网关）视觉模型逐页转写 → PageRecord 检查点。支持 `--pages/--audit-pages/--force`，内置拒答与纯拉丁文本拦截 | 活跃（独立驱动，未接入 `book_pipeline --ocr-backend`） |
| `select_ocr_samples.py` | 固定种子分层抽取 OCR 质量样本页并渲染校对切片，产出 `ocr_sample_pages.json` 供人工转写 gold | 活跃（配合发布门 `ocr.quality`） |
| `summarize_verify_report.py` | 把 `word-release-report.json`/`release-report.json` 折叠成一行结论 + 失败代码 | 活跃 |
| `note_reflow.py` | 章节注释重组（论文型 PDF 的注释定义与正文交错导入时用） | 历史，单书适配过 |
| `repair_epub_footnote_anchors.py` | 修复 EPUB noteref/锚点关系 | 历史 |

## 单书适配脚本（tools/books/）

这些脚本是针对特定书的一次性源级修复/转换，保留在仓库里作为**体例模板**
（同类书可复制改参数），不作为通用命令维护：

| 文件 | 对应书 | 功能 |
|---|---|---|
| `endnote_transform_bovary.py` | 包法利夫人 | 章末尾注 `[N]` 引用/定义 → 真 Markdown 脚注 + 读者版剪枝 |
| `endnote_transform_gender_modernity.py` | 现代性的性别 | spine 文件合并成逻辑章 + 尾注转真脚注 |
| `endnote_transform_kant.py` | 康德著作集 | 节末脚注（锚点 id 身份）转真脚注 |
| `endnote_build_reviewed_jingyudeng.py` | 镜与灯 | 生成 `reviewed_chapters/` 的 `[^n]` 闭环审定章 |
| `split_kant_volumes.py` | 康德著作集 | 导入工作目录按 part 号拆成 10 册目录 |
| `fix_kant_volume_echo_lines.py` | 康德 10 册 | 章首回显行/脚手架章源级清理 + 审计刷新 |
| `epub_cleanup_wenxue_lilun.py` | 文学理论（耶鲁） | z-lib/Duokan/WeRead 污染清单清理 |
| `scan_wenxue_fragments.py` | 文学理论（耶鲁） | 扫描残留标签碎片 |
| `clean_karatani_page_frames.py` | 柄谷行人ディスカッション | 逐页剥离版心外的印刷页码与章题跑眉，保护目录页与章首标题页 |

## 约定

- 单书脚本头部 docstring 必须写明：目标书、清理/转换的源级规则、
  审计刷新方式（markdown_sha256 / footnote 契约）。
- 修完章节 Markdown 后必须刷新 `audit/semantic-reconstruction.json`
  的摘要，否则验证门 `semantics.integrity` 失败。
- 一次性脚本不允许留在仓库根目录；`outputs/` 里也不放脚本。
- 依赖 `translation_agent` 包的工具优先用已安装环境运行；从 checkout 直接运行
  时用 `PYTHONPATH=<repo> .venv/bin/python tools/<script>.py`。
