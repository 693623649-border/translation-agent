# 英文 PDF 插件工具契约

项目目录为 tools/english_pdf_kb_plugin。DSH bundle 注册三个工具；Python 后端也可经
UTF-8 stdin 读取一个 JSON 请求，stdout 仅返回一个 JSON 对象。后端域失败返回
status=blocked 或 failed、ok=false；阶段日志不作为完成证据。

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| en_pdf_prepare | source, workspace, title, author；可选 config | 绑定真实 PDF 摘要、页数和书名作者；同一身份可重入 |
| en_pdf_status | workspace | 检查源、页检查点、计划、候选成品及回执是否仍新鲜 |
| en_pdf_run | workspace, stage；各阶段参数如下 | 分阶段处理，不并发修改同一工作区 |

en_pdf_run 的 stage 为 extract、ocr、toc、translate、plan、draft、publish、verify、
register。extract 和 ocr 必填精确的 pages 整数数组；ocr 每次至多 100 页。
translate 的 pages 可省略，省略时处理全书，配额敏感时应按确切页批次运行。
toc 使用 toc_file 导入审定 TOC，或用 toc_pages 数组调用目录模型。
plan 必填 plan_file。register 必填 layout_review_file，embedding_mode 可为 off、
auto、on。config 始终是 TOML 文件路径，密钥只通过环境或该文件引用的环境变量取得。
timeout_seconds 默认 1800，范围 1–3600。CLI 请求示例：

    {"command":"prepare","args":{"source":"E:/books/original.pdf","workspace":"E:/work/english-book","title":"核实后的中文书名","author":"核实后的编者"}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"extract","pages":[1,2,17]}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"toc","toc_file":"E:/work/reviewed-toc.json"}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"translate","pages":[17,18]}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"plan","plan_file":"E:/work/selection-plan.json"}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"draft"}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"publish"}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"verify"}}
    {"command":"run","args":{"workspace":"E:/work/english-book","stage":"register","embedding_mode":"auto","layout_review_file":"E:/work/layout-review.json"}}

## 审阅计划

在 plan 前，从 en_pdf_status 取得 source_sha256，对当前 workspace/toc.json 字节计算
toc_sha256。entries 必须覆盖 TOC 中每个 ID 恰好一次。selection 提供中文 reader_title
和对应源页审阅记录；editorial 不编入规范正文；structure 只保留层级边界。
publication_title 是读者版中文书名。下例只演示字段，真实计划必须覆盖整本目录：

    {
      "schema_version": 1,
      "source_sha256": "当前 PDF 的 SHA-256",
      "toc_sha256": "当前 toc.json 的 SHA-256",
      "publication_title": "中文选文版书名",
      "entries": [
        {"id": "toc-0001", "role": "editorial"},
        {"id": "toc-0002", "role": "selection", "reader_title": "中文篇名",
         "source_review": "PDF 第 49 页标题、正文起点与前置编者说明已对照原图"}
      ],
      "clips": [
        {"pdf_page": 49, "translated_sha256": "当前逐页译文的 SHA-256",
         "keep_from": "唯一出现的中文选文开头",
         "source_review": "源图中开头之前为编者简介"}
      ],
      "reviewed_overrides": [],
      "shared_selection_pages": [],
      "inline_editor_note_policy": "separate",
      "note_reviews": []
    }

每页只能有一条 clips。keep_from 去除该页前置材料，keep_before 去除后置材料；
二者可并用，但目标文本与摘要必须精确匹配。移出的内容和前后摘要进入
audit/editorial-clips.json，原始 pages/*.json 保留。编者材料与选文同页时必须提供
正确方向的裁切或 reviewed_chapters/<TOC-ID>.md。两篇选文确实共享同一页时，把
它们的 ID 放进 shared_selection_pages 并逐章审定，避免全局裁切损失另一篇正文。

若正文中仍有无法安全拆出的 [编者注]，可以在源页核验后写
inline_editor_note_policy=preserve_marked，同时填写非空 inline_editor_note_review。
默认 separate 会阻止含这种标签的规范知识库。源英文 [Ed.] 计数与译文标签计数
不等的页需要 note_reviews，逐页写 pdf_page 与 source_review；数量相等仍须
检查引用落点。原书分段涉及跨页首行时可提供 paragraph_layout：

    {"path":"E:/work/selection-paragraph-layout.json","sha256":"文件摘要"}

该文件的每页 starts_new_paragraph 值只约束跨页合段。单页段落错乱仍须从源图
修改审定章，而非修改 Word。

## 视觉记录与结果

verify 必须通过完整发行报告。register 所用视觉记录包含当前 DOCX 的 docx_sha256、
reviewer、实际查看的 checked_pages（Word 渲染 PDF 页号）以及具体 note：

    {"docx_sha256":"当前 Word 文件摘要","reviewer":"核验者",
     "checked_pages":[1,12,45],"note":"看过封面、章首和密集脚注页，页码与脚注位置正常"}

后端把每次出版放在 workspace/editions/<内容摘要>，旧候选留作恢复；status 返回
当前 candidate。register 是单书注册：off 只建 BM25，auto 在提供者可用时建向量，
on 要求向量成功；不会自动同步全库索引。
