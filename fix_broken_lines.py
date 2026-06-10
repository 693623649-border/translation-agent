# -*- coding: utf-8 -*-
"""
修复翻译文本中的换页截断问题。
使用 DeepSeek V4 文字核心 LLM 对全文切片进行语义修复和段落整理。
"""
import re
import sys
import io
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── 配置 ──────────────────────────────────────────────────────────
INPUT_FILE = Path("outputs/定本_完整处理/定本 柄谷行人文学论集 (柄谷行人 著，陈言 译) (z-library.sk, 1lib.sk, z-lib.sk)_20260609_155715/translation.md")
OUTPUT_FILE = Path("outputs/定本_完整处理/定本 柄谷行人文学论集 (柄谷行人 著，陈言 译) (z-library.sk, 1lib.sk, z-lib.sk)_20260609_155715/translation_fixed.md")

# DeepSeek API 配置
API_KEY = ""
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"

# 段落合并阈值：连续行数少于此值的段落将被合并
MIN_PARAGRAPH_LINES = 2

def load_env():
    """加载 .env 文件"""
    global API_KEY
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key == "DEEPSEEK_API_KEY":
                API_KEY = value
    print(f"[config] API Key: {API_KEY[:10]}..." if API_KEY else "[config] ❌ API Key 未找到", flush=True)


def call_llm(prompt: str, max_tokens: int = 4000) -> str:
    """调用 DeepSeek API"""
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是专业的日文翻译编辑，擅长修复OCR截断、整理段落结构。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = json.loads(response.read().decode("utf-8"))
                return raw["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < 2:
                print(f"  [retry] API 调用失败，重试中... ({e})")
                time.sleep(3 * (attempt + 1))
            else:
                raise RuntimeError(f"API 调用失败: {e}")


def split_by_sections(content: str) -> list[tuple[str, str]]:
    """按目录章节分割文本"""
    # 目录标题列表
    section_titles = [
        "序 文",
        "《亚历山大四重奏》的辩证法",
        "漱石试论——意识与自然",
        "意义的病——麦克白论",
        "历史与自然——森鸥外论",
        "关于坂口安吾的《日本文化私观》",
        "关于历史——武田泰淳",
        "漱石的多样性",
        "坂口安吾，其可能性的中心",
        "梦的世界——岛尾敏雄",
        "中上健次与福克纳",
        "翻译家四迷",
        "文学的衰灭",
        "初刊·底本一览",
        "译后记",
        "柄谷行人：移动的文学批评",
    ]

    lines = content.split("\n")
    sections = []
    current_section = "前言"
    current_lines = []

    for line in lines:
        stripped = line.strip()
        # 检测章节标题
        if stripped in section_titles:
            if current_lines:
                sections.append((current_section, "\n".join(current_lines)))
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_section, "\n".join(current_lines)))

    return sections


def chunk_text(text: str, max_chars: int = 8000) -> list[str]:
    """将文本按段落分割为适当大小的块"""
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > max_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [para]
            current_len = para_len
        else:
            current_chunk.append(para)
            current_len += para_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def fix_chunk(chunk: str, section_name: str) -> str:
    """使用 LLM 修复一个文本块中的截断和段落问题"""
    prompt = f"""请修复以下翻译文本中的问题：

1. **换页截断修复**：修复因PDF换页导致的不自然断行。例如：
   - "进而//言之" → "进而言之"
   - "定//本" → "定本"
   - 行尾不是标点符号，下一行突然开始新句子的情况

2. **段落整理**：将碎片化的短行合并为完整的自然段落。确保每个段落表达完整的意思。

3. **保留原意**：不要改变翻译内容，只修复格式问题。

4. **保持章节结构**：这是"{section_name}"章节的内容，请保持章节标题不变。

请直接输出修复后的文本，不要添加解释。

原文：
{chunk}"""

    return call_llm(prompt, max_tokens=8000)


def process_section(section_name: str, content: str) -> str:
    """处理一个章节的所有文本块"""
    if not content.strip():
        return content

    chunks = chunk_text(content)
    fixed_chunks = []

    for i, chunk in enumerate(chunks):
        print(f"  [fix] {section_name} 块 {i+1}/{len(chunks)} ({len(chunk):,} 字符)", flush=True)
        fixed = fix_chunk(chunk, section_name)
        fixed_chunks.append(fixed)
        time.sleep(1)  # 避免 API 限流

    return "\n\n".join(fixed_chunks)


def main():
    print("=" * 60, flush=True)
    print("  修复翻译文本中的换页截断问题", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    # 加载环境变量
    load_env()
    if not API_KEY:
        print("❌ 未找到 DEEPSEEK_API_KEY，请检查 .env 文件", flush=True)
        return

    # 读取翻译文件
    print("📖 读取翻译文件...", flush=True)
    content = INPUT_FILE.read_text(encoding="utf-8")
    print(f"   总大小: {len(content):,} 字符", flush=True)

    # 按章节分割
    print(flush=True)
    print("📐 按目录章节分割...", flush=True)
    sections = split_by_sections(content)
    print(f"   章节数: {len(sections)}", flush=True)
    for name, text in sections:
        print(f"     - {name}: {len(text):,} 字符", flush=True)

    # 修复每个章节
    print(flush=True)
    print("🔧 使用 DeepSeek V4 修复截断和段落...", flush=True)
    fixed_sections = []
    for name, text in sections:
        print(f"  [处理] {name}", flush=True)
        fixed = process_section(name, text)
        fixed_sections.append((name, fixed))

    # 重组文本
    print(flush=True)
    print("📝 重组修复后的文本...", flush=True)
    result_parts = []
    for name, text in fixed_sections:
        if name == "前言":
            continue  # 跳过前言（封面信息等）
        result_parts.append(f"## {name}\n\n{text}")

    result = "\n\n\n".join(result_parts)

    # 保存修复后的文件
    OUTPUT_FILE.write_text(result, encoding="utf-8")
    print(f"   ✅ 已保存: {OUTPUT_FILE.name}", flush=True)
    print(f"   大小: {len(result):,} 字符", flush=True)

    # 统计修复效果
    print(flush=True)
    print("📊 修复效果统计:", flush=True)
    original_broken = len(re.findall(r'//\s*$', content, re.MULTILINE))
    fixed_broken = len(re.findall(r'//\s*$', result, re.MULTILINE))
    print(f"   修复前 '//' 截断: {original_broken} 处", flush=True)
    print(f"   修复后 '//' 截断: {fixed_broken} 处", flush=True)

    print(flush=True)
    print("✅ 修复完成！", flush=True)


if __name__ == "__main__":
    main()
