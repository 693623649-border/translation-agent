# 本机 CPU PaddleOCR

`paddleocr-native` 在 macOS Apple Silicon、Linux 或有相应 PaddlePaddle CPU
安装包的 Windows 上直接运行 PP-OCRv5，不依赖 Docker、CUDA 或 OCR API 密钥。
保留原有 `paddleocr-local`（Docker NVIDIA GPU）和云端 OCR 后端。

## 安装和模型准备

在运行 CLI / Web 的同一个 Python 3.11+ 环境中执行（本机实测 Python 3.12）：

```bash
python -m pip install -e '.[web,paddle]'
translation-agent-paddle setup --variant mobile
# 可选高精度模型
translation-agent-paddle setup --variant server
translation-agent-doctor --web --paddle-native
```

模型从 Paddle 官方地址下载到 `~/.translation-agent/paddleocr/models`。
`setup` 是显式联网步骤；推理使用本地权重，不上传书页。
可用 `setup --models-dir /absolute/path` 改变下载目录，运行时同步传入
`--paddle-native-models-dir /absolute/path`。缺少依赖或权重时显式本机模式会报错，
不会偷偷切换到付费 API。`translation-agent-paddle status --variant server`
可以检查高精度模型是否就绪。

## CLI

只识别选定范围，不调用目录或翻译模型：

```bash
translation-agent run book/input.pdf -o outputs/local-ocr \
  --source-mode scanned-pdf --phase ocr --no-translate --no-verify \
  --ocr-backend paddleocr-native --paddle-native-variant mobile \
  --start-page 1 --end-page 10
```

也可在 `book_pipeline.py` 和 `graph_pipeline.py` 原有命令中传同一组 OCR 参数。
省略 `--end-page` 处理到末页。高精度版使用 `--paddle-native-variant server`。
`--paddle-native-threads` 默认 4，可设为 1–8；检测长边
`--paddle-native-det-limit` 默认 960，可设为 320–960。
竖排使用 `--ocr-reading-direction vertical`，按右列到左列排序。
阅读顺序是基于文本框的基础排序，不包含表格、复杂多栏、跨页或脚注语义重建；
这类页面必须复核，不将 OCR 成功等同于出版质量门通过。

`--ocr-backend auto` 默认依次探测 Docker GPU、本机 CPU，然后使用配置中的
云端 OCR；配置明确选择本地 Profile 时尊重该本地后端。
需要保证离线时应显式指定 `paddleocr-native`。
`pipeline.example.toml` 包含 `paddle_native` Profile，模型名支持
`PP-OCRv5-mobile` / `PP-OCRv5-server`。

完整翻译出版任务只需把 OCR 后端切到本机模式。目录、校勘和翻译阶段仍按
原来的文本模型 Profile 执行，因此那些阶段仍可能需要 API 凭据。

## Web 工作台

1. 启动 `translation-agent-web`，打开 `http://127.0.0.1:8501`。
2. “新任务”选择“扫描 PDF”，上传文件或指定工作区路径。
3. “OCR 运行方式”选择“本机 PaddleOCR（CPU）”，默认轻量版。
4. 可选“仅 OCR”，填写起止 PDF 页；结束页 0 表示全部。
5. 创建任务。在“任务状态 → 逐页 OCR 结果”查看或下载原始识别文本。

“设置”页显示两种本机模型的就绪情况。后台任务使用启动工作台的 Python
环境和当前用户的模型目录；切换环境后需在该环境重新安装依赖。
“仅 OCR”不会请求翻译或目录凭据。原始 OCR 文本标记为未校对，不能当作正式出版物。

## 资源、缓存与恢复

- 每个 OCR 阶段只加载一次模型，串行处理各页，识别 batch 固定为 1。
- 输入图像长边最多 2000，检测长边最多 960；关闭 MKL-DNN 和额外方向/矫正模型。
- 用户级内核文件锁保证多个本机 CPU OCR 任务排队使用模型，防止同时加载。
  等待任务可取消，进程退出后锁自动释放；完成阶段后释放模型与锁。
- 这些参数限制工作负载，不是操作系统级的硬内存上限。
- 每页保存后立即可恢复。身份包含权重内容、Paddle/PaddleOCR/PaddleX 版本、
  方向、线程数、检测分辨率和渲染参数；变更会使旧缓存失效。
- 空白页须通过图像空白检查；非空白图像检不出字会报错，不写入成功检查点。
- 重跑相同命令复用已完成页。Web 取消或失败的任务可点“恢复运行”。
- 原生和 Docker PaddleOCR 的页面不会被普通云端重跑静默覆盖；确实需要替换时
  使用已有的强制重跑选项。Graph 更改内容设置时仍按其已有规则主动失效缓存。

## 本机验证（2026-09-22）

M5 MacBook Air / 16 GB / Python 3.12 / PaddlePaddle 3.3.1 / PaddleOCR 3.7.0：

- 两页栅格化实际书页，轻量版约 7.7 / 5.1 秒，高精度版 Web worker 约 10.4 / 7.3 秒。
  首页面耗时包含加载；这是两页样本结果，不代表所有书籍的速度或准确率。
- CLI 输出标准 `pages/page_XXXX.json` 检查点；重跑显示 `cached=2 pending=0`。
- Web 以空凭据启动独立后台 worker，成功生成两页输出。
- 单元测试覆盖排序、资源参数、权重变化、模型复用、取消等待、空页与检查点续跑。

旧 RTX 5090 全分辨率参数在本机曾产生约 20 GB 内存占用，因此原生 CPU 后端
使用独立的有界参数，不复用 GPU 的 fp16、batch16 或原分辨率配置。
