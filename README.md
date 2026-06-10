# PDF 翻译全流程自动化管线 (Translation Agent)

一个完整的 PDF/图片 → OCR → 翻译 → 总结 → 排版（LaTeX/PDF/Word）自动化管线，支持断点续传、OCR 恢复和语义修复。

## 功能特性

- 🔄 **断点续传**: 每页保存检查点，中断后自动续传
- 📸 **OCR 恢复**: 从已有图片重建检查点
- 🔧 **语义修复**: 使用 LLM 修复 OCR 截断问题
- 📄 **多格式输出**: LaTeX → PDF / Word
- 📊 **实时监控**: 进度可视化仪表盘
- 🌐 **多 LLM 支持**: deepseek / mimo / seed

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

## Agent 数据流

```
┌─────────────┐
│  输入 PDF   │
└──────┬──────┘
       │
       ▼
┌─────────────┐     ┌─────────────┐
│  OCR Agent  │────▶│  翻译 Agent │
│  (mimo)     │     │  (deepseek) │
└──────┬──────┘     └──────┬──────┘
       │                    │
       ▼                    ▼
┌─────────────┐     ┌─────────────┐
│  检查点文件  │     │  翻译文本    │
│  _checkpoints│    │  .md        │
└──────┬──────┘     └──────┬──────┘
       │                    │
       │                    ▼
       │              ┌─────────────┐
       │              │  修复 Agent  │
       │              │  (deepseek) │
       │              └──────┬──────┘
       │                     │
       │                     ▼
       │              ┌─────────────┐
       │              │  修复后文本  │
       │              │  _fixed.md  │
       │              └──────┬──────┘
       │                     │
       ▼                     ▼
┌─────────────────────────────────┐
│         排版 Agent              │
│  LaTeX → PDF                    │
│  Word                           │
└─────────────────────────────────┘
```

### Agent 职责说明

| Agent | 职责 | 输入 | 输出 |
|-------|------|------|------|
| **OCR Agent** | 将 PDF/图片转换为文本 | PDF/图片文件 | `_checkpoints/page_XXXX.json` |
| **翻译 Agent** | 将 OCR 文本翻译为目标语言 | OCR 文本 | `translation.md` |
| **修复 Agent** | 修复 OCR 截断问题 | 截断的文本 | `translation_fixed.md` |
| **排版 Agent** | 生成 LaTeX/PDF/Word 文档 | 修复后的文本 | `.tex` / `.pdf` / `.docx` |
| **监控 Agent** | 实时监控任务进度 | 进程信息 | 进度仪表盘 |

## 文件结构

```
translation-agent/
├── pdf_text_agent.py      # 主程序（OCR + 翻译 + 总结）
├── build_docx_latex.py    # 排版脚本（LaTeX/PDF/Word）
├── fix_broken_lines.py    # 语义修复脚本
├── monitor.py             # 监控脚本
├── requirements.txt       # 依赖
├── .env.example           # 环境变量示例
├── .gitignore             # Git 忽略规则
└── README.md              # 项目说明
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 API Key
```

### 3. 运行翻译

```bash
# 首次运行（完整流程）
python pdf_text_agent.py "原文\your_file.pdf" -o outputs --llm-core deepseek --ocr-llm-core mimo

# 断点续传
python pdf_text_agent.py "原文\your_file.pdf" -o outputs --resume --llm-core deepseek --ocr-llm-core mimo

# OCR 恢复（从已有图片）
python pdf_text_agent.py "原文\your_file.pdf" -o outputs --resume --ocr-recover --llm-core deepseek --ocr-llm-core mimo --skip-translation --skip-summary
```

### 4. 生成文档

```bash
# 生成 LaTeX/PDF/Word
python build_docx_latex.py
```

### 5. 修复 OCR 截断

```bash
# 修复截断问题
python fix_broken_lines.py

# 重新生成文档
python build_docx_latex.py
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

## 配置说明

### API 配置 (`.env`)

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

### 命令行参数

```bash
python pdf_text_agent.py [选项] 输入文件

选项:
  -o, --output-dir          输出目录 (默认: outputs)
  --target-language         目标语言 (默认: 简体中文)
  --llm-core                翻译 LLM 核心 (deepseek/mimo/seed)
  --ocr-llm-core            OCR LLM 核心 (mimo/seed)
  --resume                  断点续传
  --resume-from DIR         指定续传目录
  --ocr-recover             OCR 恢复模式
  --skip-ocr                跳过 OCR
  --skip-translation        跳过翻译
  --skip-summary            跳过总结
  --keep-page-images        保留页面图片
  --start-page N            起始页码
  --max-pages N             最大页数
```

## 性能参考

| 任务 | 耗时 | 说明 |
|------|------|------|
| OCR (190 页) | ~2 小时 | 使用 mimo |
| 翻译 (190 页) | ~1 小时 | 使用 deepseek |
| 语义修复 | ~30 分钟 | 使用 deepseek |
| 总计 | ~3.5 小时 | 完整流程 |

## 依赖

```txt
pymupdf>=1.23.0
python-docx>=1.0.0
Pillow>=10.0.0
reportlab>=4.0.0
openai>=1.0.0
```

## 许可证

MIT License
