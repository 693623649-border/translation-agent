# Xiaomi-OCR-0 本地 GPU 部署(vLLM / RTX 5090 Docker)

本目录部署 [SeerRay-Lab/Xiaomi-OCR-0](https://huggingface.co/SeerRay-Lab/Xiaomi-OCR-0)([GitHub](https://github.com/SeerRay-Lab/Xiaomi-OCR-0),Apache-2.0)，只供扫描 PDF 入库对硬失败页或已校准低置信页复识一次。PP-OCRv6 medium 始终是主结果；Xiaomi 文本单独保存在复核报告中，不会自动替换或拼接。

## 模型与选型

| 项 | 选择 | 原因 |
|---|---|---|
| 模型 | SeerRay-Lab/Xiaomi-OCR-0(0.8B VLM,BF16,~1.75GB) | 基于 Qwen3.5-0.8B 的 OCR 专用统一模型:整页文档转 Markdown、文本区域 OCR、表格 OTSL、公式 LaTeX;OmniDocBench v1.6 96.83 |
| 推理引擎 | vLLM 离线批推理(`LLM.chat()`,无常驻 HTTP 服务) | 官方管线即 vLLM 路线;批量页一次提交,引擎内部并行,显著快于 transformers 逐页 generate |
| 镜像 | `vllm/vllm-openai:v0.27.1`(本机已有,零构建) | 已验证含 `Qwen3_5ForConditionalGeneration` 原生注册(vllm 0.27.1 / transformers 5.15.0 / torch 2.13.0+cu130,兼容 RTX 5090 sm_120) |
| 权重 | HF 直连下载(本机可达;`--endpoint https://hf-mirror.com` 备用) | 仅 6 个必需文件:config/model.safetensors/processor_config/tokenizer×2/chat_template |

与 PaddleOCR 的分工:PaddleOCR(PP-OCRv6 medium)是逐行检测+识别主管线；Xiaomi-OCR-0 是生成式 VLM，仅对指定疑难页复识一次。任何字符差异都用同一空白归一化 CER 比较，任意标点字形、数量或顺序差异单独触发人工核图。未校准差异阈值时，不同文本保守进入人工核图。

## 与官方管线的一致性

`batch_ocr.py` 从官方仓库(github.com/SeerRay-Lab/Xiaomi-OCR-0,Apache-2.0)移植了以下环节,保证识别行为与官方对齐:

- **任务 prompt**:逐字采用官方 `pipeline/e2e_img2md.py` 的 `PROMPTS`(text/document/table/formula);
- **图像预处理**:官方 `fit_image`——RGB、LANCZOS 等比缩到 2048×2048 以内;
- **解码参数**:temperature 0(贪心)+ top_p 0.8 + `chat_template_kwargs={"enable_thinking": False}` 非思考模式;输出预算默认 4096 tokens(官方 MCP 默认,上限 `--max-tokens`,上下文 16384);
- **重复崩塌守卫**:官方 `repetition_guard.analyze_repetition`(同字符长串/周期后缀/重复行检测)→ 空、截断或可疑页以 presence_penalty 重试一次;仅干净候选可标记成功,重试仍异常时将页标记失败并保留候选与原因供复核,不会把空输出当作空白页;
- **document 任务后处理**:官方数学定界符归一 + OTSL→HTML 表格转换。

## 目录说明

```
deploy/xiaomi-ocr/
├── docker-compose.yml   # xiaomi-ocr 服务(gpu profile,挂载 models/io/cache)
├── download_models.py   # 权重下载(仅标准库,断点续传,校验 size)
├── batch_ocr.py         # 容器侧批量识别(vLLM 离线引擎,产出 pages/page_NNNN.json)
├── verify_ocr.py        # 部署验证(GPU + 权重 + 识别断言)
├── models/              # 权重落地 models/SeerRay-Lab--Xiaomi-OCR-0/(git 忽略)
├── cache/               # vLLM 编译缓存挂载点(git 忽略,复跑提速 1-2 分钟)
└── io/                  # 宿主机与容器的图片/结果交换目录(git 忽略)
```

## 部署步骤

```bash
cd deploy/xiaomi-ocr

# 1. 确认镜像(本机已有;若缺失:docker pull vllm/vllm-openai:v0.27.1)
docker images vllm/vllm-openai:v0.27.1

# 2. 下载权重(约 1.75GB,断点续传;直连失败改 --endpoint https://hf-mirror.com)
python download_models.py

# 3. 验证部署：把自己有权使用的测试页放到 io/verify-page.png（该目录已忽略，不会提交）
# 若页面上有已知文字，可重复给出 --expected-substring 做识别对照。
docker compose --profile gpu run --rm \
  --volume ./verify_ocr.py:/opt/xiaomi-ocr-tools/verify_ocr.py:ro \
  xiaomi-ocr python3 /opt/xiaomi-ocr-tools/verify_ocr.py \
  --image /workspace/io/verify-page.png --expected-substring "你确认存在的文字"
```

注意:git-bash 下 docker 的 `/opt/...` 挂载参数会被 MSYS 转成 Windows 路径,务必走 Python subprocess(仓库工具已内置)。

## 流水线接入

扫描 PDF 管线不提供 Xiaomi 主后端开关。PP-OCRv6 medium 完成主识别后，只对硬失败页或已有人工标注校准的低置信页调用一次本服务；文本单独保存在 `audit/ocr-fallbacks.json`，不替换或拼接 Paddle 原文。复识分歧超过校准 CER 阈值、阈值尚未校准时出现任意文本差异、或任何标点不同，都会阻断发布并要求人工核图。此任务固定使用 `text` prompt 和一个 worker；表格/公式的 `document` 模式不属于书籍入库主路径。

## 本机实测(2026-10-11,RTX 5090 D 32GB,首日部署)

| 项 | 结果 |
|---|---|
| 权重下载 | 6 文件共 1.77GB,直连 huggingface.co 30.7MB/s(56.9s) |
| verify_ocr.py | 部署时使用本机测试图,已知文本断言通过(finish=stop,无重复崩塌) |
| 引擎首启 | 172-175s(torch.compile 62-65s 落入 cache/ 卷;profile/warmup 每次运行 ~110s) |
| 显存 | gpu_mem 0.45 → KV cache 10.97GB,单卡 16384-token 请求并发 51.8×;权重+激活仅 ~3.4GB |
| 历史端到端冒烟 | 部署时用 3 页合成 PDF 验证了渲染、vLLM 批推理和 PageRecord 导入；当前流水线只把它用作疑难页候选 |
| 当前检查点 | 保存在 `audit/ocr-fallbacks.json` 并绑定源 PDF、主模型、页图和原始 OCR；不会写入或覆盖 `PageRecord.text` |

注:以上是 2026-10-11 的本机部署记录,不是可由仓库复现的准确率基准;测试图未随项目提交,运行 `verify_ocr.py` 必须自行提供图片,没有 `--expected-substring` 时只验证部署链路。冒烟中两页"行尾缺失"经查为合成 PDF `insert_text` 超页宽截断(源 PDF 本身无该文本),模型识别与源完全一致。密集 A4 页预计 10-20s/页,整本书建议 `--xiaomi-workers 2` 分片。

## 常见问题

- **首跑慢**:vLLM 首次启动做 torch.compile + CUDA graph 捕获(1-2 分钟),缓存落在挂载的 `cache/`,后续 run 显著加快;
- **显存**:`gpu_memory_utilization` 默认 0.45(单分片);双分片自动降为 0.40;与 PaddleOCR 容器错峰使用即可,空闲的 `sleep infinity` 容器不占显存;
- **空结果**:空、截断或重复异常的输出会记录为失败并进入复核,不能推断为视觉空白页;只有经图像核实的真正空白页才能按上游页面分类规则排除;
- **权重损坏**:删 `models/SeerRay-Lab--Xiaomi-OCR-0/` 重跑 `download_models.py`(幂等);
- **HF 直连超时**:`python download_models.py --endpoint https://hf-mirror.com`(注意 hf-mirror 的 API 会 308 回源,直连可达时优先直连)。
