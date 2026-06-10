# 对话过程总结

## 阶段 1: 任务监控 (17:14 - 17:40)

**用户需求**: 监督任务进行，显示处理速度与预计完成时间

**执行过程**:
1. 检查当前运行进程 (PID 33632)
2. 分析输出目录结构
3. 创建 `monitor.py` 监控脚本
4. 实时显示进度条和速度

**关键数据**:
- 源文件: 定本 柄谷行人文学论集 (190 页, 73.6 MB)
- LLM 配置: deepseek (翻译) / mimo (OCR)
- 初始速度: ~1 页/分钟

## 阶段 2: 任务中断与恢复 (01:11 - 13:15)

**问题**: 进程在 OCR 157/190 页时中断

**解决方案**:
1. 添加断点续传功能 (`--resume`, `--ocr-recover`)
2. 每页保存检查点到 `_checkpoints/page_XXXX.json`
3. 从已有 `_page_images/` 恢复 OCR

**修改文件**:
- `pdf_text_agent.py`: 添加 checkpoint 函数
- 支持 `--resume`, `--resume-from`, `--ocr-recover` 参数

**恢复过程**:
1. OCR 恢复: 157 张图片 → 157 个检查点 (约 2 小时)
2. 新页 OCR: 第 158-190 页 (约 30 分钟)
3. 翻译: 190 页 (约 1 小时)
4. 总结: 自动生成

## 阶段 3: 文档排版 (14:00 - 14:30)

**需求**: 按目录结构生成 LaTeX/PDF/Word

**执行过程**:
1. 分析 translation.md 结构
2. 创建 `build_docx_latex.py`
3. 严格按目录分割章节
4. 生成三种格式文档

**目录结构**:
```
序 文
Ⅰ (第一部)
  《亚历山大四重奏》的辩证法
  漱石试论——意识与自然
  意义的病——麦克白论
  历史与自然——森鸥外论
  关于坂口安吾的《日本文化私观》
  关于历史——武田泰淳
Ⅱ (第二部)
  漱石的多样性
  坂口安吾，其可能性的中心
  梦的世界——岛尾敏雄
  中上健次与福克纳
  翻译家四迷
  文学的衰灭
初刊·底本一览
译后记
```

## 阶段 4: 语义修复 (15:50 - 16:30)

**问题**: OCR 导致 118 处换页截断

**示例**:
```
问题: 进而//言之
修复: 进而言之
```

**解决方案**:
1. 创建 `fix_broken_lines.py`
2. 使用 DeepSeek V4 切片修复
3. 按章节分割，每块 8000 字符
4. 语义理解修复截断

**修复效果**:
- 修复前: 118 处截断
- 修复后: 3 处截断 (97.5% 改善)

## 阶段 5: 清理与优化 (16:30 - 17:00)

**执行过程**:
1. 清理中间产物 (`_checkpoints/`, `_page_images/`)
2. 删除测试目录
3. 重新生成最终文档

**最终文件**:
```
outputs/定本_完整处理/
└── 定本 柄谷行人文学论集_20260609_155715/
    ├── translation.md          # 原始翻译
    ├── translation_fixed.md    # 修复后翻译
    ├── translation_final.docx  # 最终 Word
    ├── translation.pdf         # LaTeX PDF
    ├── translation.tex         # LaTeX 源文件
    ├── extracted_text.md       # OCR 文本
    ├── summary.md              # 总结
    └── metadata.json           # 元数据
```

## Agent 架构

### 1. 主程序 Agent (`pdf_text_agent.py`)

**职责**: OCR + 翻译 + 总结

**核心类**:
```python
class LLMAgent:
    def chat()           # 文本对话
    def ocr_image()      # 图片 OCR
    def translate_page()  # 页面翻译
    def summarize_chunk() # 分块总结
```

**流程**:
```
输入 → OCR → 翻译 → 总结 → 输出
```

### 2. 排版 Agent (`build_docx_latex.py`)

**职责**: 生成 LaTeX/PDF/Word

**核心函数**:
```python
def load_translation()     # 加载翻译
def split_by_sections()    # 按目录分割
def build_latex()          # 生成 LaTeX
def build_docx()           # 生成 Word
```

### 3. 修复 Agent (`fix_broken_lines.py`)

**职责**: 语义修复 OCR 截断

**核心函数**:
```python
def chunk_text()      # 文本切片
def fix_chunk()       # 单块修复
def process_section() # 章节处理
```

### 4. 监控 Agent (`monitor.py`)

**职责**: 实时监控进度

**核心函数**:
```python
def get_process_info()     # 获取进程信息
def count_page_images()    # 统计页面图片
def count_checkpoints()    # 统计检查点
def render_dashboard()     # 渲染仪表盘
```

## 数据流

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

## 关键技术点

### 1. 断点续传

**实现**: 每页保存 JSON 检查点

```python
def save_page_checkpoint(out_dir, result):
    cp_dir = out_dir / "_checkpoints"
    path = cp_dir / f"page_{result.page_number:04d}.json"
    write_json(path, asdict(result))
```

**恢复**: 加载检查点，跳过已完成页面

```python
def load_checkpoint_results(out_dir):
    cp_dir = out_dir / "_checkpoints"
    for cp_file in cp_dir.glob("page_*.json"):
        data = json.loads(cp_file.read_text())
        results[pr.page_number] = pr
```

### 2. OCR 恢复

**场景**: 中断时没有检查点，但有页面图片

**实现**: 从已有图片重新 OCR

```python
def recover_ocr_from_images(out_dir, ocr_agent, args):
    for img_path in cache_dir.glob("page_*.jpg"):
        text, notes = ocr_agent.ocr_image(img_path, ...)
        save_page_checkpoint(out_dir, result)
```

### 3. 语义修复

**场景**: OCR 导致换页截断

**实现**: 使用 LLM 理解语义修复

```python
def fix_chunk(chunk, section_name):
    prompt = f"""请修复以下文本中的截断问题：
    1. 修复 "//" 截断
    2. 合并碎片化段落
    3. 保留原意
    
    原文：{chunk}"""
    return call_llm(prompt)
```

## 性能优化

### 1. 并行处理

- OCR 和翻译可并行
- 多文件可并行处理

### 2. 缓存机制

- 页面图片缓存
- 检查点缓存
- 避免重复处理

### 3. 分块处理

- 大文本分块发送
- 控制 API 调用频率
- 避免超时

## 错误处理

### 1. API 限流

```python
def retry_call(fn, attempts=3, base_sleep=2.0):
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            time.sleep(base_sleep * attempt)
```

### 2. 进程监控

```python
def get_process_info():
    # 检查进程是否存活
    # 获取 CPU/内存使用
    # 判断是否异常
```

### 3. 文件锁处理

```python
# 检查文件是否被占用
if file.exists():
    try:
        file.unlink()
    except PermissionError:
        # 使用备用文件名
        file = file.with_stem("backup")
```

## 总结

本 skill 实现了一个完整的 PDF 翻译自动化管线，具有以下特点:

1. **全流程自动化**: OCR → 翻译 → 总结 → 排版
2. **断点续传**: 支持中断恢复
3. **语义修复**: 使用 LLM 修复 OCR 问题
4. **多格式输出**: LaTeX/PDF/Word
5. **实时监控**: 进度可视化

通过合理的 agent 架构设计，将复杂任务分解为多个独立模块，提高了代码的可维护性和可扩展性。
