# tools/ 工具清单

本目录收录流水线主入口之外的辅助工具。每个工具的定位与维护状态如下。

## 流水线级工具

| 文件 | 用途 | 维护状态 |
|---|---|---|
| `local_paddleocr_import.py` | 渲染 PDF → Docker GPU PaddleOCR 分片识别 → 导入 PageRecord。支持 `--start-page/--end-page` 页范围，作为 `book_pipeline --ocr-backend paddleocr-local` 的执行体 | **活跃**（book_pipeline 内建后端依赖它） |
| `summarize_verify_report.py` | 把 `word-release-report.json`/`release-report.json` 折叠成一行结论 + 失败代码 | 活跃 |
| `note_reflow.py` | 章节注释重组（论文型 PDF 的注释定义与正文交错导入时用） | 历史，单书适配过 |
| `repair_epub_footnote_anchors.py` | 修复 EPUB noteref/锚点关系 | 历史 |

## 宿主插件（tools/*_plugin/）

DSH 宿主插件：`package.json` 声明 `dsh.bundle`，`cordis.patch.yml` 提供插入行，
入口模块导出 `apply(ctx)`。安装后其工具直接出现在会话里。

| 目录 | 用途 | 维护状态 |
|---|---|---|
| `kb_ingest_plugin/` | 源文件 → 知识库 + Word，带逐字符中文保真门 | 活跃 |
| `jp_vertical_kb_plugin/` | 竖排日语扫描书 → 精确页 OCR/翻译 → 目录与源审阅 → Word 渲染验收 → 单书注册；配套 `japanese-vertical-kb` skill | 活跃 |
| `chinese_pdf_kb_plugin/` | 中文横排扫描书 → 按原书分段 → 正文/注释分离与清洗审计 → 五字段语料与 Word 质量门 → 单书注册；配套 `chinese-pdf-body-kb` skill | 活跃 |
| `english_pdf_kb_plugin/` | 英文原文 PDF → 文字层/精确页 OCR → 选文与编者角色审阅 → 中文 Word/EPUB/五字段知识库 → 完整发行门与单书注册；配套 `english-pdf-kb` skill | 活跃 |
| `kb_qa_plugin/` | 书库问答三工具（`kb_ask` / `kb_verify_quote` / `kb_library`）+ "知识库优先"系统提示契约（先 RAG 检索取证再作答，回答逐条标明《书名》·章节出处）；检索核心 `kb_qa.py` 可独立当 CLI 用 | **活跃** |

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
| `schmitt_body_rebuild.py` | 政治的神学、政治的概念 | 从新 OCR 行坐标和源图页下注分隔线重建正文段落，注释独立归档，恢复已核验的扫描页顺序，生成五字段正文库与 Word |
| `scan_wenxue_fragments.py` | 文学理论（耶鲁） | 扫描残留标签碎片 |

## 约定

- 单书脚本头部 docstring 必须写明：目标书、清理/转换的源级规则、
  审计刷新方式（markdown_sha256 / footnote 契约）。
- 修完章节 Markdown 后必须刷新 `audit/semantic-reconstruction.json`
  的摘要，否则验证门 `semantics.integrity` 失败。
- 一次性脚本不允许留在仓库根目录；`outputs/` 里也不放脚本。
