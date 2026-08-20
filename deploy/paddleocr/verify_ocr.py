#!/usr/bin/env python3
"""PaddleOCR 本地部署验证：生成测试图 -> GPU 推理 -> 断言识别结果。

在容器内运行（docker compose run --rm paddleocr python /workspace/tools/verify_ocr.py），
也可在任何已安装 paddlepaddle-gpu 与 paddleocr 的环境（如 WSL Ubuntu）运行。

验证内容：
  1. paddlepaddle GPU 可用且识别到 RTX 5090（sm_120 需 cuda12.9 构建）
  2. 使用 models/ 下本地权重的 PP-OCRv5 检测+识别链路输出正确文本
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def check_gpu() -> None:
    import paddle

    print(f"paddle 版本: {paddle.__version__}")
    if not paddle.device.is_compiled_with_cuda():
        print("!! 当前 paddlepaddle 非 GPU 构建", file=sys.stderr)
        sys.exit(2)
    gpu_name = paddle.device.cuda.get_device_name(0) if paddle.device.cuda.device_count() else "N/A"
    print(f"GPU 设备数: {paddle.device.cuda.device_count()}，GPU0: {gpu_name}")
    if paddle.device.cuda.device_count() == 0:
        print("!! 未检测到可用 CUDA 设备", file=sys.stderr)
        sys.exit(2)


def make_test_image(path: Path) -> str:
    """生成白底黑字测试图，返回图中绘制的文本。"""
    from PIL import Image, ImageDraw, ImageFont

    text = "PADDLEOCR LOCAL DEPLOY TEST 2026"
    img = Image.new("RGB", (960, 240), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 40)
            break
    if font is None:
        font = ImageFont.load_default()
    draw.text((40, 90), text, fill="black", font=font)
    img.save(path)
    return text


def run_ocr(image: Path, models_dir: Path, variant: str) -> str:
    from paddleocr import PaddleOCR

    det = models_dir / f"PP-OCRv5_{variant}_det"
    rec = models_dir / f"PP-OCRv5_{variant}_rec"
    doc_ori = models_dir / "PP-LCNet_x1_0_doc_ori"
    kwargs = dict(
        device="gpu:0",
        det_model_dir=str(det),
        rec_model_dir=str(rec),
        use_doc_orientation_classify=bool(doc_ori.exists()),
    )
    if doc_ori.exists():
        kwargs["doc_orientation_classify_model_dir"] = str(doc_ori)
    ocr = PaddleOCR(**kwargs)
    result = ocr.predict(str(image))
    lines: list[str] = []
    for page in result:
        texts = page.get("rec_texts", []) if isinstance(page, dict) else getattr(page, "rec_texts", [])
        if texts:
            lines.extend(texts)
    return " ".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证本地 PaddleOCR 部署")
    parser.add_argument("--models-dir", default=None, help="模型权重目录（默认：脚本旁的 models/）")
    parser.add_argument("--variant", default="server", choices=["server", "mobile"], help="使用的模型变体")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    models_dir = Path(args.models_dir) if args.models_dir else here / "models"

    check_gpu()

    from paddleocr import PaddleOCR  # noqa: F401 - 提前暴露安装问题

    test_img = here / "_verify_test.png"
    expected = make_test_image(test_img)
    print(f"测试图已生成: {test_img}")

    print(f"使用 {args.variant} 变体权重推理（首次推理含显存预热，耗时偏长）...")
    t0 = time.perf_counter()
    recognized = run_ocr(test_img, models_dir, args.variant)
    elapsed = time.perf_counter() - t0
    print(f"识别结果: {recognized!r}")
    print(f"耗时: {elapsed:.1f}s")

    ok = "PADDLEOCR" in recognized.upper() and "2026" in recognized
    print("[通过] GPU 推理与本地权重链路正常" if ok else "[失败] 识别结果与预期不符", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
