# PaddleOCR 本地 GPU 部署（RTX 5090 / Docker）

本目录包含在本机（AMD Ryzen 7 9700X / RTX 5090 D 32GB / Windows + Docker Desktop WSL2 后端）部署 PaddleOCR 的全部配置：GPU 容器环境、官方模型权重的自由下载清单与脚本、部署验证脚本。

## 已验证的部署结果（2026-08-20，本机实测）

| 项 | 结果 |
|---|---|
| GPU 直通 | 容器内识别 RTX 5090 D（32GB），Compute Capability 12.0（sm_120），Driver 610.88 / Runtime CUDA 12.9 |
| Paddle GPU 计算 | 2048×2048 matmul 通过 |
| server 变体 OCR | 识别正确（含空格伪影），首次推理 3.7s（含显存预热） |
| mobile 变体 OCR | `PaddleOCR Local De ploy Test 2026`，首次推理 2.0s |
| 权重 | 5 个模型共 193MB，宿主机直连官方源下载，容器内经挂载卷加载 |

注意：paddleocr ≥3.7 的默认模型已是 PP-OCRv6 系列；使用本地 PP-OCRv5 权重目录时必须同时传 `*_model_name` 与 `*_model_dir`（verify_ocr.py 已处理），否则报 `Model name mismatch`。

## 方案要点

| 项 | 选择 | 原因 |
|---|---|---|
| 部署方式 | Docker（WSL2 后端，GPU 直通） | 与宿主隔离、可复现；备选 WSL Ubuntu 原生安装见文末 |
| 基础镜像 | `ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.3.0-gpu-cuda12.9-cudnn9.9` | RTX 5090 为 Blackwell 架构（sm_120），要求 PaddlePaddle ≥3.2 且 CUDA 12.9 构建；3.1.x 及更早版本不支持 50 系 |
| registry | 百度 CCR（官方文档现用源） | 本机直连 Docker Hub 超时；`registry.baidubce.com` 只有 2.x 旧版 |
| PaddleOCR | `paddleocr==3.7.0`（PyPI 最新） | 兼容 paddlepaddle 3.3.0 |
| 模型 | PP-OCRv5 server + mobile（det/rec）+ 文档方向分类 | 权重从官方 `paddle-model-ecology.bj.bcebos.com` 自由下载，manifest 可自行增删 |

## 目录说明

```
deploy/paddleocr/
├── Dockerfile              # 基于官方 GPU 镜像 + paddleocr
├── docker-compose.yml      # gpu / cpu 两个 profile
├── models.manifest.json    # 模型权重清单（name/url/variant，可自由增删）
├── download_models.py      # 按清单下载并解压权重到 models/（仅标准库）
├── verify_ocr.py           # 生成测试图 → GPU 推理 → 断言识别结果
├── models/                 # 权重落地目录（git 忽略，脚本生成）
└── io/                     # 宿主机与容器的图片/结果交换目录（git 忽略）
```

## 部署步骤

### 1. 拉取基础镜像并构建

```bash
cd deploy/paddleocr

# 基础镜像约 16GB，一次拉取后常驻 D 盘 Docker 数据区
docker pull ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.3.0-gpu-cuda12.9-cudnn9.9

# 构建（安装 paddleocr；国内网络走百度 pip 源）
docker compose --profile gpu up -d --build
```

### 2. 下载模型权重

宿主机或容器内均可（容器内已内置同一脚本）：

```bash
# 宿主机：全部下载（server + mobile + 方向分类）
python download_models.py --all

# 或在容器内（权重写入挂载卷，两处等价）
docker compose --profile gpu run --rm paddleocr python /opt/paddleocr-tools/download_models.py --only server

# 查看清单
python download_models.py --list
```

每个模型解压到 `models/<name>/`（含 `inference.json`），`--only` 支持按变体（server/mobile/common）或精确模型名过滤；`--force` 强制重下。新增模型只需在 `models.manifest.json` 追加条目。

### 3. 验证部署

```bash
docker compose --profile gpu run --rm paddleocr python /opt/paddleocr-tools/verify_ocr.py
# 轻量变体验证：
docker compose --profile gpu run --rm paddleocr python /opt/paddleocr-tools/verify_ocr.py --variant mobile
```

脚本会依次确认：paddlepaddle 为 CUDA 构建且识别到 GPU、本地权重链路（检测+识别）输出正确文本。通过即部署完成。

### 4. 日常使用

```bash
# 进入容器交互
docker exec -it paddleocr bash

# 批量识别 io/ 下的图片（CLI 输出 json/markdown 到 io/）
docker exec paddleocr paddleocr ocr -i /workspace/io/images --save_path /workspace/io/out

# Python API（容器内）
from paddleocr import PaddleOCR
ocr = PaddleOCR(
    device="gpu:0",
    text_detection_model_dir="/workspace/models/PP-OCRv5_server_det",
    text_recognition_model_dir="/workspace/models/PP-OCRv5_server_rec",
)
result = ocr.predict("/workspace/io/images/page.png")
```

## Docker 数据盘位置（防 C 盘爆满）

本机 Docker Desktop 已确认全部数据落在 D 盘，机制有两层：

1. `C:\Users\<user>\AppData\Local\Docker` 是指向 `D:\DockerDesktop\Docker` 的 NTFS junction（安装时配置），WSL 后端的 `docker_data.vhdx`（镜像/容器层）物理上位于 `D:\DockerDesktop\Docker\wsl\disk\`；
2. Docker Desktop 设置 `DataFolder=D:\DockerDesktop\Docker\vm-data`（`%APPDATA%\Docker\settings-store.json`），Hyper-V 语义下的 VM 数据也固定在 D 盘。

排查方法：`docker pull` 任意镜像后看 `D:\DockerDesktop\Docker\wsl\disk\docker_data.vhdx` 的修改时间是否前进；`fsutil file queryfileid` 两个路径 ID 一致即为同一文件。

## 常见问题

- **拉取镜像报 `wsarecv: An operation on a socket could not be performed...` 且 Docker Desktop 随后退出**：本机 Clash（127.0.0.1:7897）代理在长时间大文件传输时 socket 缓冲区耗尽，Docker Desktop 内置代理链崩溃。解决：Docker Desktop 容器代理改为手动模式并绕过百度 CDN（settings-store.json 已配置 `ContainersProxyHTTPMode=manual` + `ContainersOverrideProxyExclude` 含 `*.bcebos.com,*.baidubce.com`），拉取直连不再经过 Clash；其余流量仍走代理。
- **`no kernel image is available for execution on the device` / 输出全零**：使用了不含 sm_120 的构建（如 cuda12.6 镜像或 paddlepaddle-gpu ≤3.1）。必须用 `3.x-gpu-cuda12.9-*`（≥3.2.0）镜像或对应 pip 包。
- **Docker Hub 拉取超时**：本机网络直连 `registry-1.docker.io` TLS 握手超时，统一使用百度 CCR 源。
- **内存说明**：当前机器 BIOS 仅识别 1×16GB（32GB 中另一根未被检测，需断电重插/清 CMOS）；WSL2 后端下 Docker VM 动态使用宿主可见内存（约 15GB），PP-OCRv5 推理充足。`MemoryMiB` 设置仅对 Hyper-V 后端生效，对 WSL2 无影响。
- **权重目录被写坏**：删除 `models/<name>/` 后 `--force` 重下，或直接删目录重跑脚本（幂等）。

## 备选：WSL Ubuntu 原生部署（不用 Docker）

```bash
# 在 WSL Ubuntu-24.04 内：
python -m pip install paddlepaddle-gpu==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu129/
python -m pip install paddleocr==3.7.0
python verify_ocr.py --models-dir ./models   # 复用同一套脚本与权重
```

需要 WSL 内 `nvidia-smi` 可见（当前机器 Ubuntu-24.04 已配置 WSL2 GPU 直通，驱动 610.88 / CUDA 13.3 满足要求）。Docker 路径与原生路径共用 `models/` 权重与验证脚本。

## 与翻译流水线的衔接（展望）

`book_pipeline.py` / `extract_textbook_layer.py` 当前面向原生数字 PDF；扫描件可由本服务产出文本层后再进现有语义 DAG。衔接方式：容器以 `paddleocr ocr` CLI 批量输出 JSON，宿主机侧按页合并为 text layer。
