# dsh-chinese-pdf-kb

中文横排单栏扫描 PDF 的正文重建、质量检查和单书入库 bundle。配套
[chinese-pdf-body-kb skill](../../skills/chinese-pdf-body-kb/SKILL.md) 提供源书判读与审阅方法。

| 工具 | 用途 |
| --- | --- |
| `zh_pdf_prepare` | 以明确书名、作者、新工作区和源 PDF 创建处理身份；拒绝覆盖不相关产物 |
| `zh_pdf_status` | 检查页覆盖、几何记录、来源与各阶段当前有效性；检查结果不代表阅读质量 |
| `zh_pdf_run` | 运行 `ocr / reconstruct / publish / verify / register` |

OCR 需精确的 `pages` 数组，每次最多 100 页。后续阶段使用经源图核验的 `plan_file` 与
`source_review_file`；注册还需当前实际渲染的 `layout_review_file`。接口与证据格式见
[工具契约](../../skills/chinese-pdf-body-kb/references/tool-contract.md)。

原始 OCR 不被正文清洗覆盖。规范知识库保持五字段，段落定位、注释、排除材料与清洗记录分别落盘。
清洗不采用全局删长段、删方括号或删低分字的办法。字词校订需匹配原片段并记录源图证据。
Word 逐字一致性和真正的版面审阅分别检查，改了内容、计划、审阅或向量旁车后相关回执会失效。

本插件依赖 translation-agent 源码、现有 Python 依赖、Docker/PaddleOCR 与 Windows Microsoft Word。
多栏、复杂表格、跨页双栏、边注或特殊字体需要专项适配；不伪造几何或把无法识别的正文标为空白。
注册仅针对单书，默认严格中文门与向量生成，不自动同步全库，不提供 `allow_foreign` 绕过开关。
向量服务沿用知识库 CLI 的环境变量配置，不把 API 密钥放进插件或计划。

## 安装

在 DSH 会话中使用 `plugin_manager install_bundle <本目录绝对路径>`；有独立 CLI 时可用
`dsh plugin --profile desktop add <本目录绝对路径>`。桌面宿主也支持 profile `link:` 依赖及
`dsh.profile.bundles` 注册，安装后已打开的会话需要宿主重载。

解释器依次查找插件 `python` 配置、`DSH_CHINESE_PDF_PYTHON`、`DSH_KB_PYTHON`、仓库 `.venv`、
PATH `python`。`repoRoot`/`DSH_KB_REPO_ROOT` 可设置运行目录。请求通过 UTF-8 stdin JSON，输出
为单个 JSON，阶段日志走 stderr。同步调用有时间预算；长 OCR 按页缓存恢复。
取消后核对工作区与 Docker daemon 的实际作业，不能仅凭本地客户端退出就宣称 GPU 已停止。

## 验证

```text
python -m unittest tests.test_chinese_pdf_ingest tests.test_project_skills
node tools/chinese_pdf_kb_plugin/test/plugin.test.mjs
```

测试使用合成 PDF、坐标与进程替身，不调用实际书的 OCR、翻译或嵌入服务。
