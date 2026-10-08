# 插件调用与证据

DSH 工具与 CLI 使用同一后端：`tools/jp_vertical_kb_plugin/vertical_ingest.py`。
后端 stdout 返回一个 JSON 文档，执行日志走 stderr；失败保留页缓存与尝试记录。
使用已具备本仓库依赖的 Python，不创建另一套 OCR/出版实现。

## 调用顺序

```json
{"command":"prepare","args":{"source":"E:/books/book.pdf","workspace":"E:/work/book-review","title":"核验后的中文书名","author":"核验后的作者"}}
```

先 prepare，再按样本和批次调用；`pages` 从 1 开始，每次最多 100 页。
以下只是接口示例，具体页号、路径和配置必须替换为本书实际值。

```json
{"command":"run","args":{"workspace":"E:/work/book-review","stage":"ocr","pages":[2,5,16],"timeout_seconds":600}}
{"command":"run","args":{"workspace":"E:/work/book-review","stage":"translate","pages":[5,16],"config":"E:/work/translation.toml","timeout_seconds":600}}
{"command":"status","args":{"workspace":"E:/work/book-review"}}
{"command":"run","args":{"workspace":"E:/work/book-review","stage":"compile","toc_file":"E:/work/toc.json","source_review_file":"E:/work/source-review.json"}}
{"command":"run","args":{"workspace":"E:/work/book-review","stage":"verify"}}
{"command":"run","args":{"workspace":"E:/work/book-review","stage":"register","layout_review_file":"E:/work/layout-review.json"}}
```

CLI 将单个对象通过 stdin 交给 `python tools/jp_vertical_kb_plugin/vertical_ingest.py --stdin-json`。
Windows 建议用 Python `subprocess.run(..., input=json.dumps(request, ensure_ascii=False),
encoding="utf-8")` 或 UTF-8 请求文件重定向，避免控制台代码页损坏日文与中文路径。
配置只包含环境变量引用，不将密钥写入请求、审阅文件或报告。
未给 `config` 时使用仓库 `pipeline.toml`；显式指定的配置路径会保存供后续阶段复用。
向量注册沿用知识库 CLI 的嵌入服务配置与 `ZHIPU_API_KEY`，不把翻译 profile 当作嵌入配置。

`prepare` 不覆盖已有不相关工作区。同一来源和身份可重入；插件本身不自动移动或替换旧成品。
`run` 同步执行，一次调用 1–3600 秒，默认 600 秒；长任务拆成可恢复的页批次。
切换翻译配置只影响明确指定页。进程取消后检查工作区与 Docker 任务状态，不能仅凭本地进程退出
宣称 GPU 推理已经停止；保留任务日志后再恢复。

`status` 返回 `inspected` 表示检查已执行，不表示入库成功。分别查看 `geometry_complete`、
`translation_complete`、`verification_current`、`registration_current` 和 `publication_ready`。
`source_translation_pages` 表示全部非空源页；`content_required_translation_pages` 才是当前平面目录
正文范围内的必译页。`verified` 提供保留的实际渲染路径与摘要。

## 目录与源审阅

目录必须保留本书的 `book_title`、`author`、`page_offset`、`printed_pages_per_pdf_page`、`entries`。
每个文章条目提供唯一 `id`、顺序 `index`、`level`、`kind`、`source_title`、中文 `title` 和明确 `pdf_page`。
纸页码不明时 `printed_page` 为 null。初版自动入口支持单页、平面文章目录；跨页和复杂层级需专项适配，
不能强行改变源书结构以满足入口。章节范围由仓库发布器计算，不把未经支持的 `end_pdf_page` 当作裁剪指令。

先完成 OCR/校订与目录核验，再读取 `status` 的 `source_sha256`、`page_records_sha256`；
`toc_sha256` 是准备提交的目录文件字节摘要，目录还未复制进工作区时自行计算该文件 SHA-256。
源页摘要不含翻译字段，因此纯重译不要求重新声明已看过的源图。

源审阅 JSON：

```json
{
  "source_sha256":"当前源 PDF 摘要",
  "toc_sha256":"当前目录文件摘要",
  "page_records_sha256":"status 返回的源页摘要",
  "title":"与 prepare 相同的书名",
  "author":"与 prepare 相同的作者",
  "reviewer":"实际执行核验的人或代理",
  "note":"封面/版权/目录所在页、页映射锚点、抽样范围和未逐字校订的限制",
  "content_starts":[{"pdf_page":5,"note":"对照目录与本页章首核验原题、译题及起始位置"}],
  "excluded_pages":[{"pdf_page":1,"note":"对照源图确认封面，未作为正文编译"}]
}
```

数组应覆盖所有实际文章起点及所有排除页，示例省略的项目必须补齐。记录具体图像依据与决定，不填自动
生成的“已核验”。不可识别页会阻塞；`[空白页]` 必须与源图吻合。现有入口不支持把正文范围内任意
真实文本页排除，遇到这种需求先做有证据的发布器适配，不能伪造空白标记。

## 版面审阅与发布有效期

运行 `verify` 后，从实际机器报告取得渲染 PDF，检查该 PDF 并记录当前 `artifact_sha256`。
版面审阅 JSON：

```json
{
  "artifact_sha256":"status 返回的当前产物摘要",
  "rendered_pdf":"验收器实际生成的 Word 渲染 PDF 绝对路径",
  "rendered_pdf_sha256":"该 PDF 的字节摘要",
  "reviewer":"实际查看版面的人或代理",
  "note":"说明全书缩略图检查与放大检查范围，以及修复结果",
  "pages":[{"pdf_page":1,"note":"100% 放大检查封面标题、作者、字体及留白"}]
}
```

`pages` 覆盖实际渲染 PDF 的每页。准确注明缩略图/放大/逐字检查，不能将缩略图复核表述为逐字复核。
这些文件记录证据，不能代替真正看图。机器验证回执和版面文件都必须匹配当前产物；改了页文本、目录、章节、
Word 或知识库后重新运行对应验证。`register` 只注册单书，默认生成向量并执行中文门，不执行全局 SQLite 同步。
