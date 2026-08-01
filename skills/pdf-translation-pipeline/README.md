# PDF Translation Pipeline Skill

版本 2 的主线是影印版 PDF → 逐页 OCR → 结构化目录/页码偏移 → 非中文页可选 DeepSeek 翻译 → 按标题生成章节 Markdown → 无原始分页信息的 EPUB/Word / RAG JSONL / 带书签 PDF。

输入必须是方向正确的 PDF。图片 ZIP 应先过滤系统元数据文件、按自然页序排序、应用 EXIF/OSD 或人工旋转，再按一图一页合成正向 PDF。OCR 可选 Coding Plan 视觉 MCP、标准 `glm-ocr` 或本地 Tesseract；日文竖排 Tesseract 推荐 `--tesseract-language jpn_vert+eng --tesseract-psm 3`，横排改为 `jpn+eng`。

视觉 OCR 和目录结构化继续调用 GLM/Coding Plan，翻译使用独立的 DeepSeek API，不得混用 Key 或端点。推荐用 `pipeline.toml` 的 `ocr_profile`、`toc_profile`、`translation_profile` 选择端点和模型，凭据只以 `credential_env` 引用环境变量。检测到非中文 OCR 后，可在 `compile`/`all` 阶段传 `--translate-non-chinese --target-language 简体中文`。用 `--ocr-concurrency` 和 `--translation-concurrency` 分别控制两个逐页 worker 池；翻译 worker 默认 16，在 API 额度内并行，限流时只降低受影响阶段的 worker 数。默认模型为 `deepseek-v4-pro`；切换模型使用 `--translation-profile`，临时切换凭据使用 `--translation-api-key-env`，不得把原始 Key 放入 argv。

DeepSeek 官方已公告 `deepseek-chat` 和 `deepseek-reasoner` 已于北京时间 2026-07-24 23:59 停止使用；新任务默认使用 `deepseek-v4-pro`，也可显式改用仍受支持的 `deepseek-v4-flash`。

DeepSeek V4 当前默认启用思考模式；翻译请求必须显式发送 `"thinking": {"type": "disabled"}`。翻译属于确定性文本转换，关闭思考可减少延迟和 token 消耗，并提高多 worker 吞吐。

目录 JSON 是基于完整候选目录文本的单个全局请求，需在 OCR 完成后执行；不要为了增加 worker 而拆坏目录上下文。逐页 OCR 与逐页翻译则应尽可能并行并保留逐页检查点。

完整用法见仓库根目录 `README.md`，Agent 执行规则见 `skill.md`。
