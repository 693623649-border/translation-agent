---
name: pdf-translation-pipeline
description: PDF OCR + 翻译 + 排版全流程自动化管线，支持断点续传、OCR恢复、语义修复
version: 1.0.0
author: Claude Code + 用户协作
---

# PDF 翻译全流程自动化管线

## 概述

本 skill 实现从 PDF/图片 → OCR → 翻译 → 总结 → 排版（LaTeX/PDF/Word）的全流程自动化，支持断点续传和语义修复。

## 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    PDF 翻译全流程管线                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────┐ │
│  │  输入层   │───▶│  OCR层   │───▶│  翻译层  │───▶│ 排版 │ │
│  └──────────┘    └──────────┘    └──────────┘    └──────┘ │
│       │              │               │              │       │
│       ▼              ▼               ▼              ▼       │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────┐ │
│  │ PDF/图片 │    │ mimo     │    │ deepseek │    │ LaTeX│ │
│  │ 文件     │    │ (视觉)   │    │ (文字)   │    │ Word │ │
│  └──────────┘    └──────────┘    └──────────┘    └──────┘ │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                 断点续传层 (Checkpoint)               │   │
│  │  _checkpoints/page_XXXX.json  |  _page_images/     │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                 语义修复层 (LLM Fix)                  │   │
│  │  DeepSeek V4 切片修复 → 段落整理 → 语义合并           │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. OCR 层 (`pdf_text_agent.py`)

**职责**: 将 PDF/图片转换为文本

**技术栈**:
- `fitz` (PyMuPDF): PDF 页面渲染
- `mimo` (小米): 视觉 OCR
- `seed` (字节): 备选视觉 OCR

**关键参数**:
```python
OCR_CORE = "mimo"           # 视觉 LLM 核心
DPI = 220                   # 渲染分辨率
MAX_IMAGE_SIDE = 3200       # 图片最大边长
JPEG_QUALITY = 92           # JPEG 质量
```

### 2. 翻译层 (`pdf_text_agent.py`)

**职责**: 将 OCR 文本翻译为目标语言

**技术栈**:
- `deepseek`: 文字翻译核心
- 支持分段翻译，保持术语一致性

**关键参数**:
```python
TRANSLATION_CORE = "deepseek"  # 翻译 LLM 核心
TARGET_LANGUAGE = "简体中文"     # 目标语言
TRANSLATION_CHUNK_CHARS = 800000  # 分段大小
```

### 3. 总结层 (`pdf_text_agent.py`)

**职责**: 生成文档结构化总结

**输出格式**:
- 一句话概括
- 全书结构
- 章节脉络
- 核心要点
- 重要人物/术语/译名
- 论证或叙事主线

### 4. 排版层 (`build_docx_latex.py`)

**职责**: 生成 LaTeX/PDF/Word 文档

**技术栈**:
- `xelatex`: LaTeX 编译（支持中文）
- `python-docx`: Word 生成

**目录结构**:
```
├── 序 文
├── Ⅰ（第一部）
│   ├── 《亚历山大四重奏》的辩证法
│   ├── 漱石试论——意识与自然
│   ├── 意义的病——麦克白论
│   ├── 历史与自然——森鸥外论
│   ├── 关于坂口安吾的《日本文化私观》
│   └── 关于历史——武田泰淳
├── Ⅱ（第二部）
│   ├── 漱石的多样性
│   ├── 坂口安吾，其可能性的中心
│   ├── 梦的世界——岛尾敏雄
│   ├── 中上健次与福克纳
│   ├── 翻译家四迷
│   └── 文学的衰灭
├── 初刊·底本一览
└── 译后记
```

### 5. 断点续传层

**职责**: 保存处理进度，支持中断恢复

**实现机制**:
```python
# 检查点目录
_checkpoints/
├── page_0001.json  # 每页的 OCR + 翻译结果
├── page_0002.json
└── ...

# 页面图片缓存
_page_images/
├── page_0001.jpg  # 渲染的页面图片
├── page_0002.jpg
└── ...
```

**续传命令**:
```bash
# 完整续传（从上次中断处继续）
python pdf_text_agent.py "input.pdf" -o outputs --resume --llm-core deepseek --ocr-llm-core mimo

# OCR 恢复（从已有图片重建检查点）
python pdf_text_agent.py "input.pdf" -o outputs --resume --ocr-recover --llm-core deepseek --ocr-llm-core mimo --skip-translation --skip-summary
```

### 6. 语义修复层 (`fix_broken_lines.py`)

**职责**: 修复 OCR 截断的文本

**问题类型**:
```
问题: 进而//言之
修复: 进而言之

问题: 这种批判或许可以用来批判鸥外的所有历史小说。但是，不要忘掉那时鸥外所进行的最重要的反省。进而//
言之，如果少女伊知真的如上面所描述的那样
修复: 这种批判或许可以用来批判鸥外的所有历史小说。但是，不要忘掉那时鸥外所进行的最重要的反省。进而言之，如果少女伊知真的如上面所描述的那样
```

**实现**:
```python
# 切片发送给 DeepSeek V4
chunks = chunk_text(text, max_chars=8000)
for chunk in chunks:
    fixed = call_llm(f"请修复以下文本中的截断问题：\n\n{chunk}")
```

## 工作流程

### 阶段 1: OCR + 翻译

```bash
# 首次运行
python pdf_text_agent.py "原文\input.pdf" -o outputs --llm-core deepseek --ocr-llm-core mimo --keep-page-images

# 断点续传
python pdf_text_agent.py "原文\input.pdf" -o outputs --resume --llm-core deepseek --ocr-llm-core mimo --keep-page-images
```

### 阶段 2: 排版

```bash
# 生成 LaTeX/PDF/Word
python build_docx_latex.py
```

### 阶段 3: 语义修复

```bash
# 修复 OCR 截断
python fix_broken_lines.py

# 重新排版
python build_docx_latex.py
```

## 监控系统 (`monitor.py`)

**功能**: 实时监控任务进度

**监控指标**:
- OCR 进度（页数/总页数）
- 翻译进度
- 处理速度（页/分钟）
- 预计完成时间

**启动**:
```bash
python monitor.py 90  # 每 90 秒刷新
```

## 文件结构

```
翻译项目/
├── pdf_text_agent.py      # 主程序（OCR + 翻译 + 总结）
├── build_docx_latex.py    # 排版脚本（LaTeX/PDF/Word）
├── fix_broken_lines.py    # 语义修复脚本
├── monitor.py             # 监控脚本
├── reformat_translation.py # 旧版排版脚本
├── requirements.txt       # 依赖
├── .env                   # API 配置
├── 原文/                  # 源文件
│   ├── input.pdf
│   └── ...
└── outputs/               # 输出目录
    └── 定本_完整处理/
        └── 定本 柄谷行人文学论集_20260609_155715/
            ├── translation.md          # 原始翻译
            ├── translation_fixed.md    # 修复后翻译
            ├── translation_final.docx  # 最终 Word
            ├── translation.pdf         # LaTeX PDF
            ├── translation.tex         # LaTeX 源文件
            ├── extracted_text.md       # OCR 文本
            ├── summary.md              # 总结
            ├── metadata.json           # 元数据
            └── extracted_pages.json    # 页面数据
```

## API 配置 (`.env`)

```bash
# DeepSeek (翻译)
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# MIMO (OCR)
MIMO_API_KEY=xxx
MIMO_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5

# SEED (备选 OCR)
SEED_API_KEY=xxx
SEED_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/responses
SEED_MODEL=doubao-seed-2-0-lite-260215
```

## 依赖

```txt
pymupdf>=1.23.0
python-docx>=1.0.0
Pillow>=10.0.0
reportlab>=4.0.0
openai>=1.0.0
```

## 使用示例

### 示例 1: 处理一本 190 页的书

```bash
# 步骤 1: OCR + 翻译（约 3 小时）
python pdf_text_agent.py "原文\定本 柄谷行人文学论集.pdf" \
  -o outputs \
  --llm-core deepseek \
  --ocr-llm-core mimo \
  --keep-page-images

# 步骤 2: 生成文档
python build_docx_latex.py

# 步骤 3: 修复截断（约 30 分钟）
python fix_broken_lines.py

# 步骤 4: 重新生成文档
python build_docx_latex.py
```

### 示例 2: 中断后续传

```bash
# 检查进度
python monitor.py 60

# 续传
python pdf_text_agent.py "原文\定本 柄谷行人文学论集.pdf" \
  -o outputs \
  --resume \
  --llm-core deepseek \
  --ocr-llm-core mimo \
  --keep-page-images
```

### 示例 3: 从已有图片恢复 OCR

```bash
# 恢复 OCR 检查点
python pdf_text_agent.py "原文\定本 柄谷行人文学论集.pdf" \
  -o outputs \
  --resume \
  --ocr-recover \
  --llm-core deepseek \
  --ocr-llm-core mimo \
  --skip-translation \
  --skip-summary \
  --keep-page-images
```

## 注意事项

1. **API 限流**: 每次 API 调用后等待 0.2-1 秒
2. **内存管理**: 大文件处理时注意内存使用
3. **编码问题**: 确保使用 UTF-8 编码
4. **字体支持**: LaTeX 需要安装 CTeX 宏包
5. **磁盘空间**: 190 页 PDF 约需 120MB 存储空间

## 扩展点

1. **多语言支持**: 修改 `target_language` 参数
2. **新 LLM 集成**: 在 `PROVIDER_PROFILES` 中添加新配置
3. **自定义排版**: 修改 `build_docx_latex.py` 中的样式
4. **批量处理**: 支持多文件并行处理

## 版本历史

- v1.0.0: 初始版本，支持完整流程
- 支持断点续传
- 支持 OCR 恢复
- 支持语义修复
