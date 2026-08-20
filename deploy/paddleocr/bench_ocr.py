#!/usr/bin/env python3
"""PaddleOCR 推理配置基准测试：在本机 GPU 上探索最优组合。

测试矩阵（可经 --matrix 精简）：
  - 权重变体: server / mobile
  - 精度模式: paddle_fp32 / paddle_fp16（Blackwell 上 fp16 吞吐显著更高）
  - 识别批大小: text_recognition_batch_size
  - 检测分辨率: text_det_limit_side_len（密集文档页可上调）

工作负载：A4 300dpi 中英混排密集文本页（用 io/msyh.ttc 渲染），逐配置统计
吞吐、单页延迟、显存峰值、检出行数覆盖率。

用法（容器内）：
  python /opt/paddleocr-tools/bench_ocr.py --pages 8
  python /opt/paddleocr-tools/bench_ocr.py --pages 4 --matrix quick
"""

from __future__ import annotations

import argparse
import gc
import subprocess
import threading
import time
from pathlib import Path

CN_SENTENCES = [
    "知识库流水线将原始文档转换为结构化语义单元，支持多轮翻译与校对。",
    "语义有向无环图记录了章节之间的依赖关系与发布状态。",
    "翻译代理在每一轮迭代中结合术语表与上下文约束进行解码。",
    "验收门检查排版格式、脚注编号与图表引用的一致性。",
    "批量OCR的吞吐取决于检测分辨率与识别批大小的平衡。",
]
EN_SENTENCES = [
    "The pipeline converts raw documents into structured semantic units.",
    "Each chapter carries publication state and dependency metadata.",
    "The translation agent decodes with glossary and context constraints.",
    "Acceptance gates validate layout, footnotes, and figure references.",
    "OCR throughput depends on the balance of resolution and batch size.",
]


def make_page(path: Path, font_path: Path | None):
    """生成 A4 300dpi 中英混排密集文本页，返回绘制的文本行列表（作为真值）。"""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 2480, 3508  # A4 @300dpi
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    cjk = font_path and font_path.exists()
    f_cn = ImageFont.truetype(str(font_path), 46, index=0) if cjk else None
    f_en = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if Path(candidate).is_file():
            f_en = ImageFont.truetype(candidate, 42)
            break
    if f_en is None:
        f_en = ImageFont.load_default()

    gt_lines = []
    y = 160
    para_gap = 26
    line_gap = 18
    n = 0
    while y < H - 220:
        if cjk and n % 3 != 2:
            text = CN_SENTENCES[(n // 3) % len(CN_SENTENCES)]
            font = f_cn
        else:
            text = EN_SENTENCES[(n // 3) % len(EN_SENTENCES)]
            font = f_en
        draw.text((180, y), text, fill="black", font=font)
        y += (font.size if hasattr(font, "size") else 42) + line_gap
        if n % 5 == 4:
            y += para_gap
        gt_lines.append(text)
        n += 1
    img.save(path)
    return gt_lines


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def quality_vs_gt(recognized_lines: list[str], gt_lines: list[str]) -> tuple[str, str]:
    """返回（行召回率%，CER%）。行召回=真值行被编辑距离≤10%长度的识别行覆盖的比例。"""
    gt_lines = [g.replace(" ", "") for g in gt_lines]
    rec_lines = [r.replace(" ", "") for r in recognized_lines]
    hit = 0
    err = 0
    total_chars = sum(len(g) for g in gt_lines)
    used: set[int] = set()
    for g in gt_lines:
        best_d, best_j = len(g) + 1, -1
        for j, r in enumerate(rec_lines):
            if j in used:
                continue
            d = _levenshtein(g, r)
            if d < best_d:
                best_d, best_j = d, j
        if best_j >= 0 and best_d <= max(2, len(g) * 0.1):
            hit += 1
            err += best_d
            used.add(best_j)
    recall = 100 * hit / len(gt_lines) if gt_lines else 0
    cer = 100 * err / total_chars if total_chars else 0
    return f"{recall:.1f}", f"{cer:.2f}"


class VramSampler:
    def __init__(self):
        self.peak_mb = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", "0"],
                    timeout=3,
                )
                self.peak_mb = max(self.peak_mb, int(out.decode().split()[0]))
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(0.3)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


def build_ocr(models_dir: Path, det_variant: str, rec_variant: str, det_mode: str, rec_mode: str, rec_batch: int, det_len: int):
    """先经 paddleocr 封装层组装正确的管线配置，再按需以指定精度重建。

    支持检测/识别独立选择权重变体与精度。注意（sm_120 实测）：
    PP-OCRv5_server_det 在 paddle_fp16 下输出全零（Blackwell fp16 内核缺陷，
    检出 0 行）；mobile_det 与两种 rec 的 fp16 均正常。
    """
    import copy

    from paddleocr import PaddleOCR

    base = PaddleOCR(
        device="gpu:0",
        text_detection_model_name=f"PP-OCRv5_{det_variant}_det",
        text_detection_model_dir=str(models_dir / f"PP-OCRv5_{det_variant}_det"),
        text_recognition_model_name=f"PP-OCRv5_{rec_variant}_rec",
        text_recognition_model_dir=str(models_dir / f"PP-OCRv5_{rec_variant}_rec"),
        text_recognition_batch_size=rec_batch,
        text_det_limit_side_len=det_len,
        text_det_limit_type="min" if det_len <= 960 else "max",
        use_doc_orientation_classify=False,
        use_textline_orientation=False,
        use_doc_unwarping=False,
    )
    if det_mode == "paddle" and rec_mode == "paddle":
        return base
    cfg = copy.deepcopy(base._merged_paddlex_config)
    del base
    gc.collect()
    from paddlex import create_pipeline

    # SubModule 级 engine_config 逐模型生效
    cfg["SubModules"]["TextDetection"]["engine_config"] = {
        "run_mode": det_mode, "device_type": "gpu", "device_id": 0,
    }
    cfg["SubModules"]["TextRecognition"]["engine_config"] = {
        "run_mode": rec_mode, "device_type": "gpu", "device_id": 0,
    }
    return create_pipeline(config=cfg)


def run_config(name: str, models_dir: Path, det_variant: str, rec_variant: str, det_mode: str, rec_mode: str, rec_batch: int, det_len: int, page: Path, pages: int, gt_lines: list[str]):
    ocr = build_ocr(models_dir, det_variant, rec_variant, det_mode, rec_mode, rec_batch, det_len)
    # 预热（首次推理含显存预热与图编译，不计入）
    ocr.predict(str(page))
    inputs = [str(page)] * pages
    gc.collect()
    lines_total = 0
    chars_total = 0
    all_texts: list[str] = []
    with VramSampler() as vram:
        t0 = time.perf_counter()
        for result in ocr.predict(inputs):
            for r in (result if isinstance(result, list) else [result]):
                texts = r.get("rec_texts", []) if isinstance(r, dict) else getattr(r, "rec_texts", [])
                lines_total += len(texts)
                chars_total += sum(len(t) for t in texts)
                all_texts.extend(texts)
        wall = time.perf_counter() - t0
    del ocr
    gc.collect()
    recall, cer = quality_vs_gt(all_texts, gt_lines * pages)
    return {
        "配置": name,
        "页/秒": f"{pages / wall:.2f}",
        "单页ms": f"{wall * 1000 / pages:.0f}",
        "行召回%": recall,
        "CER%": cer,
        "显存MB": str(vram.peak_mb),
        "检出文本行": str(lines_total),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PaddleOCR 推理配置基准")
    parser.add_argument("--pages", type=int, default=8, help="计时阶段页数")
    parser.add_argument("--matrix", default="full", choices=["full", "quick"], help="测试矩阵规模")
    parser.add_argument("--models-dir", default="/workspace/models")
    args = parser.parse_args()

    models_dir = Path(args.models_dir)
    tools_dir = Path(__file__).resolve().parent
    page = tools_dir / "_bench_page.png"
    gt_lines = make_page(page, Path("/workspace/io/msyh.ttc"))
    print(f"测试页: {page}（A4 300dpi，真值 {len(gt_lines)} 行/页）")

    if args.matrix == "quick":
        matrix = [
            ("server-det32-rec32-b32", "server", "server", "paddle_fp32", "paddle_fp32", 32, 736),
            ("server-det32-rec16-b32", "server", "server", "paddle_fp32", "paddle_fp16", 32, 736),
            ("mobile-det16-rec16-b32", "mobile", "mobile", "paddle_fp16", "paddle_fp16", 32, 736),
            ("mobiledet16-serverrec16-b32", "mobile", "server", "paddle_fp16", "paddle_fp16", 32, 736),
        ]
    else:
        matrix = [
            ("server-det32-rec32-b32", "server", "server", "paddle_fp32", "paddle_fp32", 32, 736),
            ("server-det32-rec16-b32", "server", "server", "paddle_fp32", "paddle_fp16", 32, 736),
            ("server-det32-rec16-b16", "server", "server", "paddle_fp32", "paddle_fp16", 16, 736),
            ("mobile-det16-rec16-b16", "mobile", "mobile", "paddle_fp16", "paddle_fp16", 16, 736),
            ("mobile-det16-rec16-b32", "mobile", "mobile", "paddle_fp16", "paddle_fp16", 32, 736),
            ("mobiledet16-serverrec16-b32", "mobile", "server", "paddle_fp16", "paddle_fp16", 32, 736),
            ("mobiledet32-serverrec16-b32", "mobile", "server", "paddle_fp32", "paddle_fp16", 32, 736),
        ]

    results = []
    for name, det_v, rec_v, det_m, rec_m, batch, det_len in matrix:
        print(f"\n>>> {name}（det={det_v}/{det_m} rec={rec_v}/{rec_m} b={batch} d={det_len}）")
        try:
            results.append(run_config(name, models_dir, det_v, rec_v, det_m, rec_m, batch, det_len, page, args.pages, gt_lines))
            last = results[-1]
            print(f"    {last['页/秒']} 页/秒 | 行召回 {last['行召回%']}% | CER {last['CER%']}% | 显存 {last['显存MB']}MB")
        except Exception as exc:  # noqa: BLE001
            print(f"    失败: {exc}")
            gc.collect()

    if results:
        cols = ["配置", "页/秒", "单页ms", "行召回%", "CER%", "显存MB", "检出文本行"]
        print("\n## 基准结果（pages=%d, 真值行数/页=%d）\n" % (args.pages, len(gt_lines)))
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in results:
            print("| " + " | ".join(r[c] for c in cols) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
