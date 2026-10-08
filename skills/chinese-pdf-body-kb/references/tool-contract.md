# DSH 与 CLI 工具契约

三个工具对应后端 `tools/chinese_pdf_kb_plugin/reader_ingest.py --stdin-json`。
stdin 为单个 `{ "command": ..., "args": ... }` JSON；stdout 为单个结果对象，日志走 stderr。
Windows 使用 UTF-8，避免控制台代码页破坏路径、题名及作者。API 密钥只通过现有环境变量提供。

## 分阶段接口

| 命令 / DSH 工具 | 必填参数 | 说明 |
| --- | --- | --- |
| `prepare` / `zh_pdf_prepare` | `source,workspace,title,author` | 新建独立工作区并记录源 PDF 摘要与页数；同源同身份可重入 |
| `status` / `zh_pdf_status` | `workspace` | 读取当前证据、缺项、摘要与回执有效性；不产生人工审阅结论 |
| `run` / `zh_pdf_run` | `workspace,stage` | `stage` 是 `ocr,reconstruct,publish,verify,register` 之一 |

`run` 可给 `plan_file,source_review_file,layout_review_file`，均为文件路径。
默认计划与源审阅在工作区 `reconstruction-plan.json` 和 `source-review.json`。
`ocr` 额外必填 `pages`，为从 1 开始的精确 PDF 页数组，每次最多 100 页。
`timeout_seconds` 默认 600，范围 1–3600。离散页只合并相邻区间；不将 `[2,17]` 扩成第 2–17 页。

```json
{"command":"prepare","args":{"source":"E:/books/book.pdf","workspace":"E:/work/book-body","title":"核验后的书名","author":"核验后的作者"}}
{"command":"run","args":{"workspace":"E:/work/book-body","stage":"ocr","pages":[2,17],"timeout_seconds":600}}
{"command":"status","args":{"workspace":"E:/work/book-body"}}
{"command":"run","args":{"workspace":"E:/work/book-body","stage":"reconstruct","plan_file":"E:/work/plan.json","source_review_file":"E:/work/source-review.json"}}
{"command":"run","args":{"workspace":"E:/work/book-body","stage":"publish"}}
{"command":"run","args":{"workspace":"E:/work/book-body","stage":"verify"}}
{"command":"run","args":{"workspace":"E:/work/book-body","stage":"register","layout_review_file":"E:/work/layout-review.json"}}
```

这些路径和页码是接口示例，需换成本书的真实值。OCR 阶段使用本仓库本地水平排序路径，保留任务来源和
图像记录。文字层清楚的 PDF 可使用文本适配器；不能用没有坐标的旧 OCR 记录虚构新几何。
长任务按批次缓存恢复。超时与取消后检查本次进程树和 Docker 实际作业，不能只根据客户端退出判断推理停止。

状态查询的 `inspected` 仅表示检查已执行。分别查看 `reconstructed`、`published`、
`verification_current`、`registration_current` 和 `publication_ready`；注册之前不会宣称可交付入库已完成。
失败尝试写入 `chinese-attempts.jsonl`，本次阶段及后续旧回执会失效。OCR 工作目录及页集合记录在
工作区清单的 `ocr_jobs`，便于检查中断后的实际作业；不要并发修改同一工作区。

## 处理计划

每个 PDF 页在 `sections[].pdf_pages` 与 `excluded_pages` 中恰好出现一次。
正文页数组就是逻辑阅读顺序，可保留经核验的倒序修复；不得根据 OCR 相似标题自动调整整书顺序。
各正文页显式给出 `note_boundary_y` 与 `header_bottom_y`，单位为对应 OCR 源图的像素。
无页下注用 `null`；页边界、隔线和图示判读须依源图，不复制其他书的固定坐标。

```json
{
  "sections":[{"id":"chapter-01","title":"一、核验后的章节标题","pdf_pages":[1,2],"opening_title_aliases":["一、核验后的章节标题"]}],
  "excluded_pages":[{"pdf_page":3,"role":"blank","reason":"源图确认无印刷正文；反面透印另存"}],
  "page_overrides":[
    {"pdf_page":1,"header_bottom_y":240,"note_boundary_y":null,"note":"对照源图确认页眉范围与无页下注","opening_title_line_indices":[0]},
    {"pdf_page":2,"header_bottom_y":240,"note_boundary_y":2800,"note":"对照源图确认页下注隔线在 y=2800"}
  ],
  "corrections":[
    {"pdf_page":2,"line_index":5,"before":"需校订的原 OCR 片段","after":"对照源图的正确片段","reason":"记录具体识别误差","evidence_image_sha256":"对应源图摘要"}
  ]
}
```

`before` 必须等于指定 OCR 行的完整原文，`after` 是完整校订行；不能由模糊匹配或只填一个待改字代替。
移除整行用 `exclude_line_indices` 并在页级 `note` 说明原因；不能以空的正文 `after` 假装保留正文。
去除行内注号也需记录完整前后行。校订不得增加省略号来替代读不清的内容。
源记录不被覆盖。正文、页眉、页下注和经确认的噪声行都有明确去向，不允许静默消失。
图示、子标题与特殊分段使用计划中支持的页级行索引覆盖，保留来源信息。
支持的覆盖为 `exclude_line_indices`、`diagram_line_indices`、`heading_line_indices`、
`paragraph_start_line_indices`、`opening_title_line_indices`，以及可选 `column_left`。
同一行不可同时指定为图示和标题。`opening_title_line_indices` 只用于章首第一 PDF 页，文字必须
准确匹配该节的 `opening_title_aliases`，以免删掉正文中的同名小标题。

## 源审阅

先用 `status` 取得当前 `source_sha256`、`ocr_sha256`，对最终计划字节计算 `plan_sha256`。
OCR 来源及图像映射属于处理身份，不能随意把其他任务的图像与页文本配对。

```json
{
  "source_sha256":"当前 PDF 摘要",
  "plan_sha256":"最终计划文件摘要",
  "ocr_sha256":"status 返回的当前 OCR 摘要",
  "reviewer":"实际执行核验的人或代理",
  "note":"目录/章首、页顺序、图像区域、样本和审阅精度说明",
  "checked_pages":[{"pdf_page":1,"note":"对照本页标题与正文，核验区域及篇章起点"}],
  "low_confidence_lines":[{"pdf_page":2,"line_index":5,"note":"看源图确认此行应如何保留或校订"}]
}
```

`checked_pages` 覆盖本书全部 PDF 页，包括排除页；例中没有列出的页必须补齐。
`low_confidence_lines` 覆盖后端报告的低置信度行，正文、注释与透印可按实际角色说明处置。
审阅可由代理执行。准确写明缩略图、区域放大或逐字检查，不伪造“人工通过”，也不把源审阅文件当作额外用户审批。
来源、计划或 OCR 变化时旧审阅失效。

## 发布、版面与注册

`publish` 只消费通过重建门的当前源稿。它生成规范语料、元数据、段落旁车、Word 和发布报告，
不依赖已经出问题的旧 Word 作为新源。正文与 Word 的缺字、多字应为 0，相似度应为 1。

`verify` 在本次 Word 上执行往返检查并实际渲染。注册前的版面审阅需绑定当前
`artifact_sha256` 和验收器保留的渲染 PDF。

```json
{
  "artifact_sha256":"status 返回的当前产物摘要",
  "rendered_pdf":"验收器保留的 PDF 绝对路径",
  "rendered_pdf_sha256":"该 PDF 的摘要",
  "reviewer":"实际查看版面的人或代理",
  "note":"全书布局查看方法、放大页范围及异常处理",
  "pages":[{"pdf_page":1,"note":"放大核验封面题名、作者和字体"}]
}
```

`pages` 覆盖实际渲染的所有页。逐页缩略图扫描与放大风险页分别记录；这些字段不代替真正看图。
机器验收、版面文件、注册回执都要匹配当前文件。修改正文、章节、Word、计划或审阅后重新验相应阶段。
`register` 严格注册单书、生成向量，保留真实模型与维度，不执行全局同步或绕过外文门。
