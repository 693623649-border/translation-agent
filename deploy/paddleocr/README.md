# PaddleOCR 双 A100 部署

该目录只管理本地 OCR 运行环境。主 Agent 的通用依赖仍由仓库根目录的
`requirements.txt` 管理，PaddlePaddle/PaddleOCR 不安装进主环境，避免 CUDA
依赖影响目录、翻译和发布阶段。

版本固定为：

- PaddlePaddle GPU 3.3.0，官方 CUDA 11.8 wheel；
- PaddleOCR 3.7.0；
- 默认通用 OCR 模型 PP-OCRv6 Medium；
- PaddlePaddle static 后端，首版使用 FP32。

## 安装

宿主机需要 Linux x86_64、可用的 NVIDIA 驱动，以及 Python 3.12。安装器会在
仓库的 `work/venvs/paddleocr-cu118` 创建隔离环境，并验证 CUDA wheel 与两张
可见 GPU：

```bash
bash deploy/paddleocr/install.sh
```

如果希望把虚拟环境放在其他位置，只覆盖专用变量，不要修改系统 Python：

```bash
PADDLEOCR_VENV_DIR=/data/venvs/paddleocr-cu118 \
  bash deploy/paddleocr/install.sh
```

如果系统的 `python3` 不是 3.12，可显式指定解释器；安装器会在下载前校验版本：

```bash
PADDLEOCR_PYTHON=/usr/bin/python3.12 bash deploy/paddleocr/install.sh
```

随后把 `pipeline.local-gpu.toml` 中的 `python_executable` 改为对应解释器。
模型首次使用时由 PaddleOCR 下载；Profile 的 `runtime.model_cache_dir` 会设置
PaddleX 3.7 实际读取的 `PADDLE_PDX_CACHE_HOME`，默认缓存到
`work/paddleocr-models`。该目录与虚拟环境均已排除在 Git 之外。
socket、锁、服务日志和临时页图使用带当前 UID 的 `/tmp` 私有目录；目录权限为
`0700`、文件权限为 `0600`，并拒绝预置符号链接。

## 运行

本地服务由 `paddleocr-local` 后端按 Profile 自动拉起并保持常驻；主
进程通过本机 Unix socket 分发图片，不经过 HTTP/Base64，也不需要 OCR API Key。
两张 GPU 各运行八个独立模型实例，页面在两卡间做数据并行。OCR 完成或进程
退出后，`persistent = true` 会让服务继续存活，下一次运行直接复用已加载模型；
逐页 JSON/Markdown 检查点仍由主 Agent 原子提交。

```bash
python graph_pipeline.py "book/input.pdf" \
  -o "outputs/input" \
  --phase all \
  --config pipeline.local-gpu.toml \
  --recipe recipes/full-publication.toml
```

只做 20 页吞吐/质量冒烟测试：

```bash
python book_pipeline.py "book/input.pdf" \
  -o "outputs/paddle-smoke" \
  --phase ocr \
  --config pipeline.local-gpu.toml \
  --start-page 1 --end-page 20
```

只比较渲染与本地推理吞吐、不写流水线产物时，使用内置基准器。它会使用
独立临时 socket，预热模型，报告 pages/s、p50/p95、字符数和平均置信度，并在
结束后正常释放 GPU：

```bash
python deploy/paddleocr/benchmark.py "book/input.pdf" \
  --config pipeline.local-gpu.toml --pages 1-100 \
  --instances-per-device 8 --rec-batch 64 --workers 64 \
  --dpi 250 --max-image-side 3200 --jpeg-quality 92 \
  --output /tmp/paddle-benchmark.json
```

不显式传渲染参数时，benchmark 与正式流水线使用相同的 200 DPI、3000 最大边长
和 JPEG 质量 90；JSON 报告会记录全部渲染参数与渲染 worker 数，便于复现。

本地 OCR Profile 不声明 `credential_env`。目录解析和非中文翻译仍分别从
`GLM_TOC_API_KEY` 与 `DEEPSEEK_API_KEY` 读取凭据；原始 Key 不得写入 TOML。

需要释放两张 GPU 时，向 Profile 中的 socket 发出正常停服请求；服务会退出、
释放 GPU 锁并删除 socket：

```bash
python - <<'PY'
import os
from pathlib import Path
from local_ocr.protocol import request

response = request(
    Path(f"/tmp/translation-agent-paddleocr-{os.geteuid()}/ppocrv6-medium.sock"),
    {"op": "shutdown"},
    timeout=10,
)
print(response)
PY
```

如果不希望跨运行常驻，把 Profile 的 `persistent` 改为 `false`；此时创建服务的
OCR 进程会在阶段结束时正常停服。

常驻服务会校验完整的模型内容身份。修改任一 `content` 字段后，应先用上述
命令关闭旧服务再运行；否则客户端会因 socket 上仍是旧模型身份而明确拒绝，
不会静默复用错误模型。只改 `runtime` 不会造成身份拒绝或页面失效，
但常驻服务不会热重载；要应用新的实例数、batch 或设备列表，仍需停服后重启。
逐页 checkpoint 另行绑定 DPI、最大图片边长和 JPEG 质量；修改这些渲染参数会
重新 OCR 相关页面，但会继续复用内容身份相同的常驻模型服务。

## 调优顺序

先固定 100 页代表集，逐项记录 pages/s、p95 延迟、GPU 利用率、显存、漏行率
和阅读顺序错误。先调整不改变文字内容的部署吞吐参数，再单独测试会刷新页面
checkpoint 的渲染参数：

1. `instances_per_device`: 1、2、4、8；仅在独占 80GB A100 时继续测试 12、16；
2. `text_recognition_batch_size`: 8、16、32、64；
3. `queue_depth`: 16、32、64；
4. Profile 顶层 `concurrency`: 通常从总模型实例数的 2 倍、4 倍开始测试；
5. 渲染 DPI: 200、250、300（每次变化会自动重做对应页面 OCR）。

`runtime` 只改变部署吞吐，不改变逐页 OCR 内容身份，因此调 worker、batch、队列
或 GPU 编号不会使既有页面失效。模型名、引擎、精度、方向模块、阅读顺序算法
等 `content` 字段，以及 DPI、最大图片边长、JPEG 质量等渲染参数会进入逐页
缓存身份；修改后会自动刷新语义陈旧页。

OCR 页面彼此独立，所以应使用双卡数据并行，不设置 tensor parallel。首轮不要
启用 TensorRT：PP-OCRv6 官方 A100 基准中 PaddlePaddle 后端已非常快，先测出
稳定的 `paddle_static` 基线，再决定是否引入额外引擎。

本机在 250 DPI、最大边长 3200、JPEG 质量 92 下，针对《中产阶级的孩子们》
代表页的实测中，每卡 1、2、4、8、12、16
实例的纯推理吞吐依次约为 3.10、5.10、7.19、8.64、8.84、9.01 页/秒。
8 实例之后收益已低于 3%，而启动时间、显存和 p95 延迟继续上升，因此默认
Profile 采用每卡 8 实例、总并发 64；16 实例/卡只适合服务已经常驻且追求
极限稳态吞吐的独占 GPU 场景。不同版式应重新运行上述 benchmark，不应把该
组数字当作所有书籍的固定 SLA。

相关官方资料：

- [PaddlePaddle pip 安装](https://www.paddlepaddle.org.cn/documentation/docs/en/install/pip/linux-pip_en.html)
- [PaddleOCR 安装](https://www.paddleocr.ai/latest/en/version3.x/installation.html)
- [PP-OCRv6](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)
- [多设备与多进程并行推理](https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/local_inference/parallel_inference.html)
