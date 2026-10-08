# dsh-jp-vertical-kb

竖排日语扫描书的分阶段入库入口，复用本仓库 PaddleOCR、逐页翻译、出版验收和单书注册。
配套工作方法在 [japanese-vertical-kb](../../skills/japanese-vertical-kb/SKILL.md)。

| DSH 工具 | 必填参数 | 作用 |
| --- | --- | --- |
| `jp_vertical_prepare` | `source, workspace, title, author` | 记录 PDF 摘要与页数；拒绝覆盖不相关工作区 |
| `jp_vertical_status` | `workspace` | 报告 OCR、几何记录、逐页译文、摘要及缺项 |
| `jp_vertical_run` | `workspace, stage` | 执行 `ocr / translate / compile / verify / register` |

`ocr` 和 `translate` 还需 `pages`：从 1 开始的精确页号数组，每次最多 100 页。
离散页只合并相邻区间，切换模型不会扩大重试范围。`timeout_seconds` 默认 600，范围 1–3600；
按页缓存恢复。超时/取消后查看 `vertical-attempts.jsonl`、页记录和 Docker 作业状态再继续。

`compile` 需核验目录和源审阅记录；`verify` 运行完整出版验收与实际 Word 渲染；`register` 还需
与当前产物及实际渲染结果匹配的版面审阅。证据格式、CLI 示例和适用边界见
[工具契约](../../skills/japanese-vertical-kb/references/tool-contract.md)。
审阅文件是实际核验的记录，程序不会代替操作者看图，也不能证明全书语义无误。

支持单页扫描的平面文章目录。复杂分部、跨页扫描、同页多篇及正文范围内非空分隔页的排除需要专门适配；
入口会阻止不支持的结构。此版本不自动全库同步，不提供语言门绕过参数，不改写原始 OCR 来假造空白页。
新任务使用新的独立工作区，验收后再决定旧版替换；已有普通流水线工作区不能直接冒充插件工作区。

## 安装与运行

这是依赖 translation-agent 源码的本地链接 bundle，不是可脱离仓库运行的 npm 包。
当前完整验收路径需要 Windows、Microsoft Word，以及已配置的本地 Docker/PaddleOCR；翻译与向量注册
分别使用项目的模型服务和嵌入服务。`config` 默认为仓库 `pipeline.toml`，显式配置路径会在工作区中保留，
供后续阶段复用；它不改变 `knowledge_base_cli register` 使用的 `ZHIPU_API_KEY` 嵌入配置。
在 DSH 会话内安装：

```text
plugin_manager install_bundle <本目录的绝对路径>
```

有独立 DSH CLI 的环境也可运行 `dsh plugin --profile desktop add <本目录绝对路径>`。
Windows 桌面版可能没有 PATH 中的 `dsh` 命令；使用宿主的 bundle 安装功能，或沿用已有 profile 的
`link:` 依赖和 `dsh.profile.bundles` 注册方式。需要重载插件的现有会话应在宿主重新加载后再使用新工具。

`cordis.patch.yml` 默认启用专用提示契约。配置 `python` 指向已安装项目依赖的解释器；留空时依次查看
`DSH_JP_VERTICAL_PYTHON`、`DSH_KB_PYTHON`、仓库 `.venv`、PATH 的 `python`。
可用 `repoRoot`/`DSH_KB_REPO_ROOT` 指定工作目录。请求通过 UTF-8 stdin JSON 传递，输出为 JSON，日志走 stderr。
API 密钥沿用项目的环境变量，不放进 bundle 或审阅文件。

状态查询的 `inspected` 表示已完成检查；是否可交付看 `publication_ready`，是否已注册看
`registration_current`。`verification_current` 只说明当前机器报告与产物及保留渲染匹配。
完成注册会保存版面审阅及注册凭据；源、正文、目录、成品、审阅或向量侧车变更后，相关凭据会失效。

## 验证

```text
python -m unittest tests.test_jp_vertical_ingest tests.test_project_skills
node tools/jp_vertical_kb_plugin/test/plugin.test.mjs
```

Node 测试需要现有 DSH 的 `@deepseek-ai/dsh-tools`；按本机既有依赖链接，或设置 `DSH_PROFILE_DIR`。
测试使用合成页和子进程替身，不消耗模型额度、不运行真实书的 OCR。
