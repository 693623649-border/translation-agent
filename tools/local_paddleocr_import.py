#!/usr/bin/env python3
"""Run PP-OCRv6 medium for scanned PDFs with one-time Xiaomi page fallback."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scan_layout import prepare_scan_pages

from ocr_quality_gate import (
    ocr_structure_anomalies,
    quotation_signature,  # noqa: E402
    character_error_rate,
    comparison_reasons,
    parse_ocr_notes,
    punctuation_signature,
    read_json_object,
    sha256_text,
    write_json_atomic,
)

PADDLEOCR_ROOT = REPO_ROOT / "deploy" / "paddleocr"
XIAOMI_OCR_ROOT = REPO_ROOT / "deploy" / "xiaomi-ocr"
BACKENDS = {
    "paddleocr": PADDLEOCR_ROOT,
    "xiaomi-ocr": XIAOMI_OCR_ROOT,
}
PADDLEOCR_IO_ROOT = PADDLEOCR_ROOT / "io"
MANIFEST_NAME = "local_paddleocr_job.json"
SCRIPT_VERSION = 3


def _backend_root(backend: str) -> Path:
    try:
        return BACKENDS[backend]
    except KeyError as exc:
        raise ValueError(f"Unsupported OCR backend: {backend}") from exc


def _io_root(backend: str) -> Path:
    return _backend_root(backend) / "io"


def _die(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return slug[:80] or "pdf"


def _resolve_under_io(path: Path, backend: str) -> Path:
    root = _io_root(backend).resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"--work-dir must be under {root}") from exc
    return resolved


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _page_count(pdf_path: Path) -> int:
    import fitz

    with fitz.open(pdf_path) as document:
        return int(document.page_count)


def _source_info(pdf_path: Path) -> dict[str, Any]:
    stat = pdf_path.stat()
    return {
        "path": str(pdf_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256_file(pdf_path),
        "page_count": _page_count(pdf_path),
    }


def _render_params(args: argparse.Namespace) -> dict[str, int]:
    return {
        "dpi": int(args.dpi),
        "max_side": int(args.max_side),
        "jpeg_quality": int(args.jpeg_quality),
    }


def _expected_manifest(args: argparse.Namespace, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SCRIPT_VERSION,
        "source_pdf": source,
        "render": _render_params(args),
        "reading_direction": args.reading_direction,
    }


def _manifest_reuse_matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return (
        existing.get("version") == expected["version"]
        and existing.get("source_pdf") == expected["source_pdf"]
        and existing.get("render") == expected["render"]
        and existing.get("reading_direction") == expected["reading_direction"]
    )


def _default_work_dir(pdf_path: Path, source_hash: str, backend: str) -> Path:
    return _io_root(backend) / "jobs" / f"{_safe_slug(pdf_path.stem)}-{source_hash[:12]}"


def _range_work_dir(base: Path, args: argparse.Namespace, start_page: int, end_page: int) -> Path:
    render_key = (
        f"dpi{args.dpi}-max{args.max_side}-q{args.jpeg_quality}-"
        f"{args.reading_direction}"
    )
    return base / (
        f"render-v{SCRIPT_VERSION}-{start_page:04d}-{end_page:04d}-{render_key}"
    )


def _image_path(images_dir: Path, page: int) -> Path:
    return images_dir / f"page_{page:04d}.jpg"


def _render_pages(
    pdf_path: Path,
    images_dir: Path,
    args: argparse.Namespace,
    *,
    reuse: bool,
    pages: Iterable[int],
) -> None:
    from book_pipeline import render_pdf_page

    params = _render_params(args)
    page_list = list(pages)
    pending = [
        page
        for page in page_list
        if args.force_render or not reuse or not _image_path(images_dir, page).is_file()
    ]
    if not pending:
        print(f"[render] total={len(page_list)} cached={len(page_list)} pending=0")
        return

    images_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"[render] total={len(page_list)} cached={len(page_list) - len(pending)} "
        f"pending={len(pending)}"
    )

    def render_one(page: int) -> None:
        target = _image_path(images_dir, page)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        render_pdf_page(
            pdf_path,
            page,
            temp,
            dpi=params["dpi"],
            max_side=params["max_side"],
            quality=params["jpeg_quality"],
        )
        os.replace(temp, target)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(render_one, page): page for page in pending}
        for future in concurrent.futures.as_completed(futures):
            page = futures[future]
            future.result()
            if page == pending[-1] or len(pending) <= 20:
                print(f"[render-page] page={page}", flush=True)


def _validate_images(images_dir: Path, pages: Iterable[int]) -> None:
    missing = [page for page in pages if not _image_path(images_dir, page).is_file()]
    if missing:
        preview = ", ".join(str(page) for page in missing[:20])
        raise RuntimeError(f"missing rendered page images: {preview}")


def _docker_path(host_path: Path, backend: str) -> str:
    rel = host_path.resolve().relative_to(_io_root(backend).resolve())
    return "/workspace/io/" + rel.as_posix()


def _model_id(args: argparse.Namespace, source_pdf_sha256: str = "") -> str:
    if getattr(args, "backend", "paddleocr") == "paddleocr":
        from book_pipeline import paddle_local_model_id

        return paddle_local_model_id(
            dpi=args.dpi,
            max_image_side=args.max_side,
            reading_direction=args.reading_direction,
            jpeg_quality=args.jpeg_quality,
            source_pdf_sha256=source_pdf_sha256,
        )
    from book_pipeline import xiaomi_local_model_id

    return xiaomi_local_model_id(
        task=args.xiaomi_task,
        dpi=args.dpi,
        max_image_side=args.max_side,
        reading_direction=args.reading_direction,
        jpeg_quality=args.jpeg_quality,
        source_pdf_sha256=source_pdf_sha256,
    )


def _paddle_ocr_command(work_dir: Path, args: argparse.Namespace, model_id: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--profile",
        "gpu",
        "run",
        "--rm",
        "--volume",
        f"{PADDLEOCR_ROOT / 'batch_ocr.py'}:/opt/paddleocr-tools/batch_ocr.py:ro",
        "--volume",
        f"{PADDLEOCR_ROOT / 'bench_ocr.py'}:/opt/paddleocr-tools/bench_ocr.py:ro",
        "paddleocr",
        "python",
        "/opt/paddleocr-tools/batch_ocr.py",
        "--images-dir",
        _docker_path(work_dir / "images", "paddleocr"),
        "--output-dir",
        _docker_path(work_dir, "paddleocr"),
        "--models-dir",
        "/workspace/models",
        "--model-id",
        model_id,
        "--reading-direction",
        args.reading_direction,
    ]


def _xiaomi_ocr_command(work_dir: Path, args: argparse.Namespace, model_id: str) -> list[str]:
    gpu_mem = args.xiaomi_gpu_mem
    if gpu_mem is None:
        # Two concurrent shards must share the 32GB card headroom.
        gpu_mem = 0.40 if args.workers >= 2 else 0.45
    return [
        "docker",
        "compose",
        "--profile",
        "gpu",
        "run",
        "--rm",
        # The vllm image entrypoint is the `vllm` CLI; run plain python instead.
        "--entrypoint",
        "python3",
        "--volume",
        f"{XIAOMI_OCR_ROOT / 'batch_ocr.py'}:/opt/xiaomi-ocr-tools/batch_ocr.py:ro",
        "xiaomi-ocr",
        "/opt/xiaomi-ocr-tools/batch_ocr.py",
        "--images-dir",
        _docker_path(work_dir / "images", "xiaomi-ocr"),
        "--output-dir",
        _docker_path(work_dir, "xiaomi-ocr"),
        "--models-dir",
        "/workspace/models",
        "--task",
        args.xiaomi_task,
        "--max-tokens",
        str(args.xiaomi_max_tokens),
        "--gpu-memory-utilization",
        str(gpu_mem),
        "--model-id",
        model_id,
        "--reading-direction",
        args.reading_direction,
    ]


def _run_ocr(
    work_dir: Path,
    args: argparse.Namespace,
    model_id: str,
    *,
    tolerate_page_errors: bool = False,
) -> bool:
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    base_cmd = (
        _paddle_ocr_command(work_dir, args, model_id)
        if args.backend == "paddleocr"
        else _xiaomi_ocr_command(work_dir, args, model_id)
    )
    if args.force_ocr:
        base_cmd.append("--force")

    processes: list[tuple[int, subprocess.Popen[bytes]]] = []
    for index in range(args.workers):
        cmd = [
            *base_cmd,
            "--shard-index",
            str(index),
            "--shard-count",
            str(args.workers),
        ]
        print(f"[ocr-start] shard={index + 1}/{args.workers}", flush=True)
        processes.append(
            (
                index,
                subprocess.Popen(cmd, cwd=_backend_root(args.backend)),
            )
        )

    failed: list[tuple[int, int]] = []
    for index, process in processes:
        code = process.wait()
        if code:
            failed.append((index, code))
    if failed:
        details = ", ".join(f"shard {index} exit {code}" for index, code in failed)
        if not tolerate_page_errors:
            backend_label = "PaddleOCR" if args.backend == "paddleocr" else "Xiaomi-OCR"
            raise RuntimeError(f"{backend_label} failed: {details}")
        print(f"[ocr-warning] page-level failures will be examined: {details}", flush=True)
    return bool(failed)


def _load_page_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return value


def _validate_pages(
    work_dir: Path,
    pages: Iterable[int],
    model_id: str,
    *,
    allow_hard_failures: bool = False,
) -> list[int]:
    pages_dir = work_dir / "pages"
    page_list = list(pages)
    expected_names = {f"page_{page:04d}.json" for page in page_list}
    actual_names = {path.name for path in pages_dir.glob("page_*.json")}
    extra_names = sorted(actual_names - expected_names)
    if extra_names:
        raise RuntimeError(f"unexpected OCR page files: {extra_names[:20]}")

    missing: list[int] = []
    mismatched: list[int] = []
    blank: list[int] = []
    for page in page_list:
        path = pages_dir / f"page_{page:04d}.json"
        if not path.is_file():
            missing.append(page)
            continue
        item = _load_page_json(path)
        if item.get("pdf_page") != page:
            raise RuntimeError(f"{path} has pdf_page={item.get('pdf_page')!r}, expected {page}")
        if item.get("ocr_model") != model_id:
            mismatched.append(page)
        notes = parse_ocr_notes(item.get("notes"))
        if not str(item.get("text") or "").strip() or notes.get("hard_failure"):
            blank.append(page)
    if missing or mismatched or (blank and not allow_hard_failures):
        parts = []
        if missing:
            parts.append(f"missing={missing[:20]}")
        if mismatched:
            parts.append(f"model_mismatch={mismatched[:20]}")
        if blank:
            parts.append(f"blank_text={blank[:20]}")
        raise RuntimeError("invalid OCR page coverage: " + " ".join(parts))
    return sorted(set(missing + blank))


def _record_hard_failures(work_dir: Path, pages: Iterable[int], model_id: str) -> None:
    """Keep failed pages explicit so neither import nor release can treat them as blank."""

    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        path = pages_dir / f"page_{page:04d}.json"
        item = _load_page_json(path) if path.is_file() else {
            "pdf_page": page,
            "text": "",
            "language": "unknown",
            "ocr_model": model_id,
        }
        notes = parse_ocr_notes(item.get("notes"))
        image = work_dir / "images" / f"page_{page:04d}.jpg"
        notes.update(
            {
                "engine": "paddleocr-local",
                "hard_failure": True,
                "source_image": image.name,
                "source_image_sha256": _sha256_file(image) if image.is_file() else "",
            }
        )
        item["notes"] = json.dumps(notes, ensure_ascii=False, separators=(",", ":"))
        item["text"] = str(item.get("text") or "")
        item["ocr_model"] = model_id
        _atomic_write_json(path, item)


def _mark_visually_blank_pages(work_dir: Path, pages: Iterable[int]) -> None:
    """Only classify a failed OCR result as blank when the source image is visibly blank."""

    from book_pipeline import is_visually_blank_page

    for page in pages:
        path = work_dir / "pages" / f"page_{page:04d}.json"
        image = work_dir / "images" / f"page_{page:04d}.jpg"
        if not path.is_file() or not image.is_file():
            continue
        visually_blank, ink_ratio = is_visually_blank_page(image)
        if not visually_blank:
            continue
        item = _load_page_json(path)
        notes = parse_ocr_notes(item.get("notes"))
        notes.update(
            {
                "visual_blank": True,
                "ink_ratio": round(ink_ratio, 6),
                "hard_failure": False,
            }
        )
        item["text"] = "[空白页]"
        item["language"] = "unknown"
        item["notes"] = json.dumps(notes, ensure_ascii=False, separators=(",", ":"))
        _atomic_write_json(path, item)


def _calibration(output_dir: Path, source_sha256: str, model_id: str) -> dict[str, Any]:
    value = read_json_object(output_dir / "audit" / "ocr-calibration.json")
    if (
        value.get("source_pdf_sha256") != source_sha256
        or value.get("primary_model_id") != model_id
    ):
        return {}
    return value


def select_xiaomi_fallback_pages(
    hard_failures: Iterable[int], low_confidence: Iterable[int]
) -> list[int]:
    """Run the one-time alternate OCR only for the calibrated trigger classes."""
    return sorted(set(hard_failures) | set(low_confidence))


def _run_xiaomi_fallback(
    source_sha256: str,
    primary_work_dir: Path,
    pages: list[int],
    args: argparse.Namespace,
) -> tuple[str, dict[int, dict[str, Any]], str | None]:
    fallback_args = argparse.Namespace(**vars(args))
    fallback_args.backend = "xiaomi-ocr"
    fallback_args.workers = 1
    fallback_work_dir = (
        _io_root("xiaomi-ocr")
        / "jobs"
        / f"fallback-{source_sha256[:12]}-{hashlib.sha256(','.join(map(str, pages)).encode()).hexdigest()[:10]}"
    )
    _resolve_under_io(fallback_work_dir, "xiaomi-ocr")
    images_dir = fallback_work_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        source = primary_work_dir / "images" / f"page_{page:04d}.jpg"
        if not source.is_file():
            raise RuntimeError(f"fallback source image is missing for page {page}: {source}")
        shutil.copy2(source, images_dir / source.name)
    fallback_id = _model_id(fallback_args, source_sha256)
    failure: str | None = None
    try:
        if _run_ocr(
            fallback_work_dir,
            fallback_args,
            fallback_id,
            tolerate_page_errors=True,
        ):
            failure = "one or more Xiaomi shards reported page failures"
    except Exception as exc:  # The primary OCR remains preserved; record fallback failure.
        failure = f"{type(exc).__name__}: {exc}"
    candidates: dict[int, dict[str, Any]] = {}
    for page in pages:
        path = fallback_work_dir / "pages" / f"page_{page:04d}.json"
        if not path.is_file():
            candidates[page] = {"error": failure or "Xiaomi produced no checkpoint"}
            continue
        item = _load_page_json(path)
        if item.get("ocr_model") != fallback_id:
            candidates[page] = {"error": "Xiaomi checkpoint model identity mismatch"}
            continue
        text = str(item.get("text") or "").strip()
        notes = parse_ocr_notes(item.get("notes"))
        unresolved = (
            not text
            or notes.get("hard_failure") is True
            or notes.get("status") != "passed"
            or notes.get("finish_reason") == "length"
            or (isinstance(notes.get("failure_reason"), str) and bool(notes["failure_reason"]))
        )
        if unresolved:
            candidates[page] = {
                "error": notes.get("failure_reason") or "Xiaomi candidate is empty, stale, truncated, or unresolved",
                "raw_text": text,
                "model_id": fallback_id,
                "notes": notes,
            }
        else:
            candidates[page] = {
                "text": text,
                "text_sha256": sha256_text(text),
                "model_id": fallback_id,
                "notes": notes,
            }
    return fallback_id, candidates, failure


def _previous_fallback_evidence_state(
    report: dict[str, Any],
    page_item: dict[str, Any],
    *,
    primary_text_sha256: str,
    image_sha256: str,
    expected_fallback_model_id: str,
) -> str:
    """Classify one prior attempt without repeating the one-time fallback call."""
    if (
        page_item.get("primary_text_sha256") != primary_text_sha256
        or page_item.get("image_sha256") != image_sha256
    ):
        return "stale"
    if (
        report.get("fallback_evidence_version") == 2
        and page_item.get("fallback_model_id") == expected_fallback_model_id
        and page_item.get("fallback_candidate_status") in {"passed", "failed"}
    ):
        return "current"
    return "legacy"


def _write_fallback_report(
    output_dir: Path,
    source: dict[str, Any],
    primary_model_id: str,
    fallback_model_id: str | None,
    attempted_pages: list[int],
    primary_work_dir: Path,
    fallback_candidates: dict[int, dict[str, Any]],
    calibration: dict[str, Any],
    legacy_candidates: dict[int, dict[str, Any]] | None = None,
) -> None:
    existing = read_json_object(output_dir / "audit" / "ocr-fallbacks.json")
    page_results = {
        int(item["pdf_page"]): item
        for item in existing.get("pages", [])
        if isinstance(item, dict) and isinstance(item.get("pdf_page"), int)
    } if (
        existing.get("source_pdf_sha256") == source["sha256"]
        and existing.get("primary_model_id") == primary_model_id
    ) else {}
    prepared = prepare_scan_pages([_load_page_json(path) for path in (primary_work_dir / "pages").glob("page_*.json")])
    structure = ocr_structure_anomalies({page: item["text"] for page, item in prepared.items()})
    threshold = calibration.get("disagreement_cer_threshold")
    threshold = float(threshold) if isinstance(threshold, (int, float)) else None
    legacy_candidates = legacy_candidates or {}
    pages_to_update = sorted(set(attempted_pages) | set(legacy_candidates))
    for page in pages_to_update:
        primary_path = primary_work_dir / "pages" / f"page_{page:04d}.json"
        primary = _load_page_json(primary_path)
        primary_text = str(primary.get("text") or "").strip()
        primary_notes = parse_ocr_notes(primary.get("notes"))
        fallback = fallback_candidates.get(page, legacy_candidates.get(page, {}))
        fallback_notes = parse_ocr_notes(fallback.get("notes"))
        fallback_raw_text = str(fallback.get("raw_text") or fallback.get("text") or "")
        fallback_invalid = bool(
            fallback.get("error")
            or not str(fallback.get("text") or "").strip()
            or fallback_notes.get("hard_failure") is True
            or fallback_notes.get("status") not in {None, "passed"}
            or fallback_notes.get("finish_reason") == "length"
        )
        fallback_text = "" if fallback_invalid else str(fallback.get("text") or "")
        hard_failure = bool(primary_notes.get("hard_failure"))
        reasons = list(
            comparison_reasons(
                primary_text,
                fallback_text,
                cer_threshold=threshold,
                hard_failure=hard_failure,
            )
        )
        reasons.extend(structure.get(page, []))
        if fallback_invalid:
            reasons.append("fallback_hard_failure")
        cer = character_error_rate(primary_text, fallback_text) if fallback_text else None
        page_results[page] = {
                "pdf_page": page,
                "image_sha256": primary_notes.get("source_image_sha256", ""),
                "primary_text": primary_text,
                "primary_text_sha256": sha256_text(primary_text),
                "primary_hard_failure": hard_failure,
                "primary_confidence": primary_notes.get("score_avg"),
                "fallback_text": fallback_text,
                "fallback_text_sha256": fallback.get("text_sha256", "") if not fallback_invalid else "",
                "fallback_candidate_raw_text": fallback_raw_text,
                "fallback_candidate_notes": fallback_notes,
                "fallback_candidate_status": fallback_notes.get("status") or ("failed" if fallback.get("error") else "missing"),
                "fallback_model_id": fallback.get("model_id", fallback_model_id or ""),
                "fallback_error": fallback.get("error", ""),
                "cer": cer,
                "punctuation_diff": bool(
                    fallback_text
                    and punctuation_signature(primary_text)
                    != punctuation_signature(fallback_text)
                ),
                "review_required": bool(reasons),
                "reasons": reasons,
            }
    payload = {
        "schema_version": 2,
        "fallback_evidence_version": 2,
        "source_pdf": source["path"],
        "source_pdf_sha256": source["sha256"],
        "primary_model_id": primary_model_id,
        "fallback_model_id": fallback_model_id or existing.get("fallback_model_id"),
        "calibration_used": bool(calibration),
        "attempted_pages": sorted(set(existing.get("attempted_pages", [])) | set(attempted_pages)),
        "review_pages": sorted(page for page, item in page_results.items() if item["review_required"]),
        "pages": [page_results[page] for page in sorted(page_results)],
    }
    write_json_atomic(output_dir / "audit" / "ocr-fallbacks.json", payload)


def _import_records(
    work_dir: Path, output_dir: Path, pages: Iterable[int]
) -> None:
    from book_pipeline import import_existing_ocr

    expected = len(list(pages))
    imported = import_existing_ocr(work_dir, output_dir)
    if imported != expected:
        raise RuntimeError(f"imported {imported} pages, expected {expected}")
    print(f"[import] pages={imported} output={output_dir}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a PDF, run the PP-OCRv6 primary, and import page checkpoints."
    )
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-side", type=int, default=3000)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--xiaomi-task", choices=("text", "document"), default="text")
    parser.add_argument("--xiaomi-max-tokens", type=int, default=4096)
    parser.add_argument("--xiaomi-gpu-mem", type=float, default=None)
    parser.add_argument("--reading-direction", choices=("horizontal", "vertical", "auto"), default="horizontal")
    parser.add_argument("--force-render", action="store_true")
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int, default=None)
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--ocr-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    args.backend = "paddleocr"
    if args.render_only and args.ocr_only:
        return _die("--render-only and --ocr-only are mutually exclusive")
    if args.dpi <= 0 or args.max_side <= 0 or args.jpeg_quality <= 0:
        return _die("--dpi, --max-side, and --jpeg-quality must be positive")

    source_pdf = args.source_pdf.resolve()
    if not source_pdf.is_file():
        return _die(f"source PDF not found: {source_pdf}")
    if source_pdf.suffix.lower() != ".pdf":
        return _die(f"source is not a PDF: {source_pdf}")

    try:
        source = _source_info(source_pdf)
        base_work_dir = (
            _default_work_dir(source_pdf, str(source["sha256"]), args.backend)
            if args.work_dir is None
            else _resolve_under_io(args.work_dir, args.backend)
        )
        page_count = int(source["page_count"])
        start_page = int(args.start_page)
        end_page = int(args.end_page or page_count)
        if not 1 <= start_page <= end_page <= page_count:
            return _die(
                f"--start-page/--end-page must satisfy 1 <= start <= end <= {page_count}"
            )
        pages = range(start_page, end_page + 1)
        if args.work_dir is None:
            work_dir = _range_work_dir(base_work_dir, args, start_page, end_page)
        else:
            work_dir = base_work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = work_dir / MANIFEST_NAME
        expected_manifest = _expected_manifest(args, source)
        reuse = _manifest_reuse_matches(manifest_path, expected_manifest)
        if manifest_path.exists() and not reuse and not args.force_render:
            return _die(
                f"manifest mismatch at {manifest_path}; use --force-render or a different --work-dir"
            )

        images_dir = work_dir / "images"
        if page_count < 1:
            return _die("PDF has no pages")

        if not args.ocr_only:
            _render_pages(
                source_pdf, images_dir, args, reuse=reuse, pages=pages
            )
            _validate_images(images_dir, pages)
            _atomic_write_json(manifest_path, expected_manifest)
        else:
            if not reuse:
                return _die(f"--ocr-only requires a matching manifest at {manifest_path}")
            _validate_images(images_dir, pages)

        if args.render_only:
            print(
                f"[done] rendered={len(pages)} pages={start_page}-{end_page} "
                f"work_dir={work_dir}"
            )
            return 0

        model_id = _model_id(args, str(source["sha256"]))
        primary_shard_failure = _run_ocr(
            work_dir,
            args,
            model_id,
            tolerate_page_errors=(args.backend == "paddleocr"),
        )
        if args.backend == "paddleocr":
            if primary_shard_failure and any(
                not (work_dir / f"batch_summary_shard_{index:02d}.json").is_file()
                for index in range(args.workers)
            ):
                raise RuntimeError(
                    "PP-OCRv6 failed before writing shard summaries; no page-level "
                    "fallback is safe because the primary engine did not complete startup."
                )
            page_list = list(pages)
            pages_dir = work_dir / "pages"
            missing = [
                page
                for page in page_list
                if not (pages_dir / f"page_{page:04d}.json").is_file()
            ]
            # If the model failed before writing a page checkpoint, preserve an
            # explicit empty primary result. It can only be released after a
            # human supplies a visually checked correction.
            _record_hard_failures(work_dir, missing, model_id)
            hard_failures = _validate_pages(
                work_dir, page_list, model_id, allow_hard_failures=True
            )
            _mark_visually_blank_pages(work_dir, hard_failures)
            hard_failures = _validate_pages(
                work_dir, page_list, model_id, allow_hard_failures=True
            )
            calibration = _calibration(args.output_dir.resolve(), str(source["sha256"]), model_id)
            confidence_threshold = calibration.get("confidence_threshold")
            low_confidence: list[int] = []
            if isinstance(confidence_threshold, (int, float)) and not isinstance(confidence_threshold, bool):
                for page in page_list:
                    if page in hard_failures:
                        continue
                    item = _load_page_json(pages_dir / f"page_{page:04d}.json")
                    score = parse_ocr_notes(item.get("notes")).get("score_avg")
                    if isinstance(score, (int, float)) and score <= confidence_threshold:
                        low_confidence.append(page)
            prepared = prepare_scan_pages([_load_page_json(pages_dir / f"page_{page:04d}.json") for page in page_list])
            structure = ocr_structure_anomalies({page: item["text"] for page, item in prepared.items()})
            write_json_atomic(args.output_dir.resolve() / "audit" / "ocr-structure-issues.json", {
                "schema_version": 1,
                "source_pdf_sha256": source["sha256"],
                "primary_model_id": model_id,
                "status": "human_review_required" if structure else "passed_structure_signals",
                "pages": structure,
                "note": "Structural anomalies route to image review; they do not trigger extra Xiaomi calls.",
            })
            fallback_pages = select_xiaomi_fallback_pages(hard_failures, low_confidence)
            fallback_id: str | None = None
            candidates: dict[int, dict[str, Any]] = {}
            previous = read_json_object(args.output_dir.resolve() / "audit" / "ocr-fallbacks.json")
            legacy_candidates: dict[int, dict[str, Any]] = {}
            if previous.get("source_pdf_sha256") == source["sha256"] and previous.get("primary_model_id") == model_id:
                fallback_args = argparse.Namespace(**vars(args))
                fallback_args.backend = "xiaomi-ocr"
                expected_fallback_id = _model_id(fallback_args, str(source["sha256"]))
                previous_pages = {
                    int(item["pdf_page"]): item
                    for item in previous.get("pages", [])
                    if isinstance(item, dict) and isinstance(item.get("pdf_page"), int)
                }
                bound_pages: set[int] = set()
                for page in fallback_pages:
                    old_item = previous_pages.get(page)
                    page_path = pages_dir / f"page_{page:04d}.json"
                    if old_item is None or not page_path.is_file():
                        continue
                    current = _load_page_json(page_path)
                    current_text_sha = sha256_text(str(current.get("text") or "").strip())
                    current_image_sha = parse_ocr_notes(current.get("notes")).get("source_image_sha256", "")
                    evidence_state = _previous_fallback_evidence_state(
                        previous, old_item,
                        primary_text_sha256=current_text_sha,
                        image_sha256=current_image_sha,
                        expected_fallback_model_id=expected_fallback_id,
                    )
                    if evidence_state == "stale":
                        continue
                    # An old-schema attempt still consumes the one-time retry; preserve it
                    # and require image review instead of silently reusing or calling again.
                    bound_pages.add(page)
                    if evidence_state == "legacy":
                        legacy_candidates[page] = {
                            "error": "legacy_fallback_evidence_unverified",
                            "raw_text": old_item.get("fallback_candidate_raw_text") or old_item.get("fallback_text") or "",
                            "model_id": old_item.get("fallback_model_id", ""),
                            "notes": {"status": "legacy_unresolved", "hard_failure": True},
                        }
                fallback_pages = [page for page in fallback_pages if page not in bound_pages]
            else:
                legacy_candidates = {}
            if fallback_pages:
                try:
                    fallback_id, candidates, _failure = _run_xiaomi_fallback(
                        str(source["sha256"]), work_dir, fallback_pages, args
                    )
                except Exception as exc:  # Preserve the primary output and block unresolved pages later.
                    candidates = {
                        page: {"error": f"{type(exc).__name__}: {exc}"}
                        for page in fallback_pages
                    }
            _write_fallback_report(
                args.output_dir.resolve(),
                source,
                model_id,
                fallback_id,
                fallback_pages,
                work_dir,
                candidates,
                calibration,
                legacy_candidates=legacy_candidates,
            )
        else:
            _validate_pages(work_dir, pages, model_id)
        _import_records(work_dir, args.output_dir.resolve(), pages)
        print(f"[done] imported={len(pages)} model={model_id}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
