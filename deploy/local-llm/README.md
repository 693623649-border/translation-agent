# 本地 vLLM 翻译后端（deploy/local-llm）

本分支（`deploy/vllm-translation-rtx5090`）把翻译内核从 DeepSeek 云 API 切换为
本机 vLLM 部署的 **Hy-MT2-30B-A3B（INT4 量化，Intel AutoRound 版）**。
同一套代码保留配置切回 DeepSeek 的能力；本分支 `pipeline.toml` 默认走本地。

## 边界（与设计共识一致）

- 只替换**翻译阶段**：`book_pipeline` 主链、`semantic_translation_runner`（EPUB）、
  `kb_translation`（`translate-kb` 入库翻译）、Web 工作台 EPUB 路径。
- **OCR 校勘（proofread）仍走 DeepSeek**（`pipeline.toml` 的 `proofread_profile = "deepseek_flash"`）：
  校勘不是翻译，混元 MT 特化模型不适合。想连校勘也本地化，把它改成 `local_hymt2`。
- **embedding 不动**：RAG 向量继续走智谱 `embedding-3` 云 API。
- **不自动回退**：local 失败即报错重试，不存在悄悄切回 DeepSeek 的路径。
- `pdf_text_agent.py`（旧入口，README 标注非首选）未接入。

## 前置条件

- Docker Desktop + WSL2（GPU 直通已在本机验证：`wsl nvidia-smi` 可见 5090）。
- 镜像 `vllm/vllm-openai:v0.27.1`（与 `deploy/xiaomi-ocr` 同一个，本机已有）。

## 首次部署

```powershell
cd deploy/local-llm

# 1. 下载权重（约 16GB，落到 ./models/；默认走 hf-mirror.com）
docker compose --profile gpu run --rm download
#   若镜像站也失败：在宿主机设置代理后重试，例如
#   $env:HTTPS_PROXY="http://127.0.0.1:7890"; docker compose --profile gpu run --rm download

# 2. 常驻启动（占约 17GB 显存；剩余约 15GB 给 PaddleOCR）
docker compose --profile gpu up -d

# 3. 验证
curl http://127.0.0.1:8010/v1/models
```

首次启动 vLLM 要编译 CUDA graph（约 1-2 分钟），之后 `cache/` 复用编译产物。

## 与流水线对接

- 本分支 `pipeline.toml` 已默认 `translation_profile = "local_hymt2"`，
  jp_vertical / english_pdf 流水线无需任何参数即为本地翻译。
- 切回云端：`pipeline.toml` 里 `[pipeline].translation_profile` 改回 `"deepseek_flash"`（一行）。
- 知识库入库翻译：`translation-agent-kb translate-kb <path> --provider local`
- EPUB / CLI 直用：`--translation-provider local` 或 env `TRANSLATION_PROVIDER=local`；
  端点/模型可用 `LOCAL_LLM_API_BASE` / `LOCAL_LLM_MODEL` 覆盖（见 `.env.example`）。
- vLLM 无鉴权，`LOCAL_LLM_API_KEY` 留空即可；若服务以 `--api-key` 启动则填真实值。

## 显存预算

| 组件 | 占用 |
| --- | --- |
| Hy-MT2-30B-A3B INT4 权重 | ~16 GB |
| KV cache（`--gpu-memory-utilization 0.92`，max-model-len 32768） | ~14 GB 池上限 |
| PaddleOCR（OCR 阶段） | ~2-4 GB |

同一本书 OCR 与翻译是先后阶段，不叠加；并行处理多本书时如遇 OOM，
把 `pipeline.toml` 里 `local_hymt2.concurrency` 从 16 调低，或临时 `docker compose down`。

## 故障排查

- **vLLM 报架构不支持（Hy 非标准 Qwen 架构）**：升级镜像 tag
  （`docker-compose.yml` 里 `vllm/vllm-openai:v0.27.1` → 更新版本）。模型 2026-05 发布，
  v0.27.1 大概率已含支持（vllm issue #46043）。
- **AutoRound 量化加载失败**：在 `vllm serve` 命令里补 `--quantization` 参数
  （AutoRound 默认导出 GPTQ 兼容格式，通常可写 `gptq`）。
- **换模型**：改 `docker-compose.yml` 的下载仓库名 + `--served-model-name`，
  再同步 `pipeline.toml` 的 `local_hymt2.model`（profile 与 served-model-name 必须一致）。
- **端口冲突**：8010 被占时改 compose 端口映射 + `pipeline.toml` 的 `local_hymt2.base_url`。

## 单元测试与冒烟

- 单测：`python -m pytest tests/test_local_translation_provider.py -q`
  （payload 兼容层、无凭据可用、身份指纹与云端隔离）。
- 端到端冒烟：服务起来后运行
  `python deploy/local-llm/smoke_translate.py`——
  走 `pipeline.toml → build_translation_client → ChatTranslator` 的真实装配路径
  翻译一段含『』「」的日文，校验 LocalClient、回环端点、中文输出与引号序列。
