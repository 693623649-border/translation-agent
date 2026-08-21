#!/usr/bin/env python3
"""Render a PDF, run local PaddleOCR in Docker, and import PageRecords."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PADDLEOCR_ROOT = REPO_ROOT / "deploy" / "paddleocr"
PADDLEOCR_IO_ROOT = PADDLEOCR_ROOT / "io"
MANIFEST_NAME = "local_paddleocr_job.json"
SCRIPT_VERSION = 1


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


def _resolve_under_io(path: Path) -> Path:
    root = PADDLEOCR_IO_ROOT.resolve()
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
    )


def _default_work_dir(pdf_path: Path, source_hash: str) -> Path:
    return PADDLEOCR_IO_ROOT / "jobs" / f"{_safe_slug(pdf_path.stem)}-{source_hash[:12]}"


def _image_path(images_dir: Path, page: int) -> Path:
    return images_dir / f"page_{page:04d}.jpg"


def _render_pages(
    pdf_path: Path,
    images_dir: Path,
    page_count: int,
    args: argparse.Namespace,
    *,
    reuse: bool,
) -> None:
    from book_pipeline import render_pdf_page

    params = _render_params(args)
    pending = [
        page
        for page in range(1, page_count + 1)
        if args.force_render or not reuse or not _image_path(images_dir, page).is_file()
    ]
    if not pending:
        print(f"[render] cached={page_count} pending=0")
        return

    images_dir.mkdir(parents=True, exist_ok=True)
    print(f"[render] total={page_count} cached={page_count - len(pending)} pending={len(pending)}")

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


def _validate_images(images_dir: Path, page_count: int) -> None:
    missing = [page for page in range(1, page_count + 1) if not _image_path(images_dir, page).is_file()]
    if missing:
        preview = ", ".join(str(page) for page in missing[:20])
        raise RuntimeError(f"missing rendered page images: {preview}")


def _docker_path(host_path: Path) -> str:
    rel = host_path.resolve().relative_to(PADDLEOCR_IO_ROOT.resolve())
    return "/workspace/io/" + rel.as_posix()


def _model_id(args: argparse.Namespace) -> str:
    return (
        "paddleocr-local/"
        f"PP-OCRv5-{args.det_variant}-det-{args.det_mode}-"
        f"{args.rec_variant}-rec-{args.rec_mode}-"
        f"b{args.rec_batch}-det{args.det_len}-"
        f"dpi{args.dpi}-max{args.max_side}-v1"
    )


def _run_ocr(work_dir: Path, args: argparse.Namespace, model_id: str) -> None:
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    base_cmd = [
        "docker",
        "compose",
        "--profile",
        "gpu",
        "run",
        "--rm",
        "paddleocr",
        "python",
        "/opt/paddleocr-tools/batch_ocr.py",
        "--images-dir",
        _docker_path(work_dir / "images"),
        "--output-dir",
        _docker_path(work_dir),
        "--models-dir",
        "/workspace/models",
        "--det-variant",
        args.det_variant,
        "--rec-variant",
        args.rec_variant,
        "--det-mode",
        args.det_mode,
        "--rec-mode",
        args.rec_mode,
        "--rec-batch",
        str(args.rec_batch),
        "--det-len",
        str(args.det_len),
        "--model-id",
        model_id,
    ]
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
                subprocess.Popen(cmd, cwd=PADDLEOCR_ROOT),
            )
        )

    failed: list[tuple[int, int]] = []
    for index, process in processes:
        code = process.wait()
        if code:
            failed.append((index, code))
    if failed:
        details = ", ".join(f"shard {index} exit {code}" for index, code in failed)
        raise RuntimeError(f"PaddleOCR failed: {details}")


def _load_page_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return value


def _validate_pages(work_dir: Path, page_count: int, model_id: str) -> None:
    pages_dir = work_dir / "pages"
    expected_names = {f"page_{page:04d}.json" for page in range(1, page_count + 1)}
    actual_names = {path.name for path in pages_dir.glob("page_*.json")}
    extra_names = sorted(actual_names - expected_names)
    if extra_names:
        raise RuntimeError(f"unexpected OCR page files: {extra_names[:20]}")

    missing: list[int] = []
    mismatched: list[int] = []
    blank: list[int] = []
    for page in range(1, page_count + 1):
        path = pages_dir / f"page_{page:04d}.json"
        if not path.is_file():
            missing.append(page)
            continue
        item = _load_page_json(path)
        if item.get("pdf_page") != page:
            raise RuntimeError(f"{path} has pdf_page={item.get('pdf_page')!r}, expected {page}")
        if item.get("ocr_model") != model_id:
            mismatched.append(page)
        if not str(item.get("text") or "").strip():
            blank.append(page)
    if missing or mismatched or blank:
        parts = []
        if missing:
            parts.append(f"missing={missing[:20]}")
        if mismatched:
            parts.append(f"model_mismatch={mismatched[:20]}")
        if blank:
            parts.append(f"blank_text={blank[:20]}")
        raise RuntimeError("invalid OCR page coverage: " + " ".join(parts))


def _import_records(work_dir: Path, output_dir: Path, page_count: int) -> None:
    from book_pipeline import import_existing_ocr

    imported = import_existing_ocr(work_dir, output_dir)
    if imported != page_count:
        raise RuntimeError(f"imported {imported} pages, expected {page_count}")
    print(f"[import] pages={imported} output={output_dir}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a PDF, run local PaddleOCR Docker shards, and import OCR pages."
    )
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-side", type=int, default=3000)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--det-variant", choices=("server", "mobile"), default="server")
    parser.add_argument("--rec-variant", choices=("server", "mobile"), default="server")
    parser.add_argument("--det-mode", default="paddle_fp32")
    parser.add_argument("--rec-mode", default="paddle_fp16")
    parser.add_argument("--rec-batch", type=int, default=16)
    parser.add_argument("--det-len", type=int, default=736)
    parser.add_argument("--force-render", action="store_true")
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--ocr-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
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
        work_dir = (
            _default_work_dir(source_pdf, str(source["sha256"]))
            if args.work_dir is None
            else _resolve_under_io(args.work_dir)
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = work_dir / MANIFEST_NAME
        expected_manifest = _expected_manifest(args, source)
        reuse = _manifest_reuse_matches(manifest_path, expected_manifest)
        if manifest_path.exists() and not reuse and not args.force_render:
            return _die(
                f"manifest mismatch at {manifest_path}; use --force-render or a different --work-dir"
            )

        images_dir = work_dir / "images"
        page_count = int(source["page_count"])
        if page_count < 1:
            return _die("PDF has no pages")

        if not args.ocr_only:
            _render_pages(source_pdf, images_dir, page_count, args, reuse=reuse)
            _validate_images(images_dir, page_count)
            _atomic_write_json(manifest_path, expected_manifest)
        else:
            if not reuse:
                return _die(f"--ocr-only requires a matching manifest at {manifest_path}")
            _validate_images(images_dir, page_count)

        if args.render_only:
            print(f"[done] rendered={page_count} work_dir={work_dir}")
            return 0

        model_id = _model_id(args)
        _run_ocr(work_dir, args, model_id)
        _validate_pages(work_dir, page_count, model_id)
        _import_records(work_dir, args.output_dir.resolve(), page_count)
        print(f"[done] imported={page_count} model={model_id}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
