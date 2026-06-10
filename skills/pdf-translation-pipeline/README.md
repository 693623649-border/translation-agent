# PDF 翻译全流程自动化管线

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API
cp .env.example .env
# 编辑 .env 填入 API Key

# 3. 运行翻译
python pdf_text_agent.py "原文\your_file.pdf" -o outputs --llm-core deepseek --ocr-llm-core mimo

# 4. 生成文档
python build_docx_latex.py
```

## 核心功能

### 1. OCR + 翻译
- 支持 PDF/图片输入
- 使用 mimo 进行视觉 OCR
- 使用 deepseek 进行翻译
- 自动生成总结

### 2. 断点续传
- 每页保存检查点
- 中断后自动续传
- 支持 OCR 恢复

### 3. 排版输出
- LaTeX → PDF（高质量排版）
- Word（可编辑文档）
- 严格按目录结构

### 4. 语义修复
- 修复 OCR 截断
- 整理段落结构
- 使用 DeepSeek V4

## 文件说明

| 文件 | 说明 |
|------|------|
| `pdf_text_agent.py` | 主程序（OCR + 翻译 + 总结） |
| `build_docx_latex.py` | 排版脚本 |
| `fix_broken_lines.py` | 语义修复脚本 |
| `monitor.py` | 监控脚本 |

## 使用场景

1. **学术翻译**: 翻译学术论文、专著
2. **文档数字化**: 将纸质文档转换为可编辑格式
3. **批量处理**: 大规模文档翻译
4. **断点续传**: 长时间任务中断恢复

## 技术栈

- **OCR**: mimo (小米视觉 LLM)
- **翻译**: deepseek (深度求索)
- **排版**: xelatex + python-docx
- **监控**: 自定义 Python 脚本

## 性能参考

- 190 页 PDF: 约 3 小时完成全流程
- OCR 速度: 约 1 页/分钟
- 翻译速度: 约 1 页/分钟
- 语义修复: 约 30 分钟

## 许可证

MIT License
