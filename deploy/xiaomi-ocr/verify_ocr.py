#!/usr/bin/env python3
"""Verify the local Xiaomi-OCR-0 deployment (GPU + weights + recognition).

Container usage (supply a test image mounted under /workspace/io):
  python3 /opt/xiaomi-ocr-tools/verify_ocr.py --image /workspace/io/verify-page.png

Runs the supplied image through the same vLLM engine used by batch_ocr.py.
Optional --expected-substring values compare recognition with operator-known
text; without them this is only a deployment smoke test, not an OCR accuracy test.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from batch_ocr import PROMPTS, analyze_repetition, postprocess_output, run_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify local Xiaomi-OCR-0 GPU deployment.")
    parser.add_argument("--models-dir", type=Path, default=Path("/workspace/models"))
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--expected-substring", action="append", default=[], help="known text that must appear; may be repeated")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.45)
    parser.add_argument("--max-num-seqs", type=int, default=4)
    args = parser.parse_args()

    import torch

    if not torch.cuda.is_available():
        print("error: CUDA is not available in this container", flush=True)
        return 1
    props = torch.cuda.get_device_properties(0)
    print(f"[gpu] {props.name} ({props.total_memory / 1e9:.1f} GB, sm_{props.major}{props.minor})", flush=True)

    if not args.image.is_file():
        print(f"error: test image not found: {args.image}", flush=True)
        return 1

    from batch_ocr import build_engine

    print("[engine] loading Xiaomi-OCR-0 via vLLM ...", flush=True)
    engine = build_engine(args.models_dir, args.gpu_memory_utilization, args.max_num_seqs)

    results = run_batch(engine, [args.image], PROMPTS["text"], max_tokens=1024, presence_penalty=0.0)
    item = results[0]
    text = postprocess_output(item["text"], "text")
    print(f"[result] finish={item['finish_reason']} tokens={item['completion_tokens']}")
    print("[result] text:")
    print(text)

    if not text.strip():
        print("error: OCR returned empty text; this is not a blank-page decision", flush=True)
        return 1
    if analyze_repetition(text, finish_reason=item["finish_reason"])["suspicious"]:
        print("error: output is truncated or flagged for repetition collapse", flush=True)
        return 1
    missing = [token for token in args.expected_substring if token.lower() not in text.lower()]
    if missing:
        print(f"error: expected substrings missing from recognition: {missing}", flush=True)
        return 1
    if not args.expected_substring:
        print("[note] no text ground truth supplied; only the deployment path was smoke-tested")
    print("[ok] local Xiaomi-OCR-0 deployment verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
