"""Headless DOCX render gate used by the publication verifier.

Package/XML checks cannot detect layout failures caused by a Word-compatible
renderer or font substitution.  This module converts a DOCX to a temporary PDF
with an isolated LibreOffice profile, then performs deterministic page-level
sanity checks.  The temporary PDF is never a publication artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
from typing import Any
import zipfile
import xml.etree.ElementTree as ET

import fitz
from PIL import Image, ImageChops


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{WORD_NS}}}"

_BIBLIOGRAPHY_AUTHOR_PATTERN = (
    r"[A-Z][A-Za-zÀ-ɏ’'\-]{1,35},\s+"
    r"(?:[A-Z](?:\.|[a-zÀ-ɏ’'\-]{1,24}))"
    r"(?:\s+[A-Z](?:\.|[a-zÀ-ɏ’'\-]{1,24})){0,2}"
)
_BIBLIOGRAPHY_AUTHOR_AT_START = re.compile(
    rf"^\s*{_BIBLIOGRAPHY_AUTHOR_PATTERN}"
)
_BIBLIOGRAPHY_AUTHOR_AFTER_ENTRY_END = re.compile(
    rf"\b(?:18|19|20)\d{{2}}\s*[.。;；]\s+"
    rf"(?P<author>{_BIBLIOGRAPHY_AUTHOR_PATTERN})(?=\s*,)"
)
_BIBLIOGRAPHY_AUTHOR_AFTER_COLON = re.compile(
    rf"[:：]\s+(?P<author>{_BIBLIOGRAPHY_AUTHOR_PATTERN})(?=\s*,)"
)
_INDEX_ALIAS = re.compile(r"[（(][^()（）\n]{1,120}[）)]")
_INDEX_ALIAS_WITH_REFERENCES = re.compile(
    r"[（(][^()（）\n]{1,120}[）)]\s*"
    r"\d{1,4}(?:\s*(?:[,，、;；]\s*\d{1,4}|[–—-]\s*\d{1,4}))+"
)
_INDEX_REFERENCE_RUN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\d{1,4}(?:\s*(?:[,，、;；]\s*\d{1,4}|[–—-]\s*\d{1,4}))+"
    r"(?![A-Za-z0-9])"
)


def find_soffice() -> str | None:
    candidates = [
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/lib/libreoffice/program/soffice",
    ]
    return next((value for value in candidates if value and Path(value).is_file()), None)


_WORD_RENDER_SCRIPT = (
    Path(__file__).resolve().parent
    / "skills"
    / "docx-publication-finisher"
    / "scripts"
    / "render_docx_with_word.ps1"
)


def _render_docx_with_word(path: Path, *, timeout_seconds: int) -> dict[str, Any] | None:
    """Render via the isolated Word COM fallback when LibreOffice is absent.

    Returns ``None`` when this Windows-only fallback is unavailable so the
    caller keeps its renderer-missing diagnostic; otherwise returns the
    rendered-PDF verdict using the same page inspection as LibreOffice.
    """

    if os.name != "nt" or not _WORD_RENDER_SCRIPT.is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="docx-render-word-") as directory:
        output_dir = Path(directory) / "output"
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_WORD_RENDER_SCRIPT),
            "-DocxPath",
            str(path),
            "-OutputDirectory",
            str(output_dir),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "failed",
                "issues": [
                    _issue("docx_render_failed", f"{type(exc).__name__}: {exc}")
                ],
                "warnings": [],
                "metrics": {
                    "path": str(path),
                    "renderer": "microsoft-word-isolated",
                },
            }
        # Windows PowerShell writes the console in the OEM code page; decode
        # tolerantly because success is proven by the exit code and the PDF
        # artifact, not by parsing the JSON report.
        stdout_text = (completed.stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (completed.stderr or b"").decode("utf-8", errors="replace")
        try:
            payload = json.loads(stdout_text.strip() or "{}")
        except ValueError:
            payload = {}
        pdf_path = output_dir / f"{path.stem}.pdf"
        if completed.returncode != 0 or not pdf_path.is_file():
            return {
                "status": "failed",
                "issues": [
                    _issue(
                        "docx_render_failed",
                        "隔离 Word 渲染器未生成有效 PDF。",
                        returncode=completed.returncode,
                        stderr=(stderr_text or stdout_text).strip()[-1200:],
                    )
                ],
                "warnings": [],
                "metrics": {
                    "path": str(path),
                    "renderer": "microsoft-word-isolated",
                    "renderer_report": payload,
                },
            }
        inspected = inspect_rendered_pdf(
            pdf_path,
            expected_text=_docx_visible_text(path),
        )
        return {
            "status": "failed" if inspected["issues"] else "passed",
            "issues": list(inspected["issues"]),
            "warnings": list(inspected["warnings"]),
            "metrics": {
                "path": str(path),
                "renderer": "microsoft-word-isolated",
                **inspected["metrics"],
            },
        }


def _issue(code: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "evidence": evidence}


def _canonical_characters(value: str) -> str:
    return "".join(
        re.findall(r"[A-Za-z0-9\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value)
    )


def _docx_visible_text(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for member in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
            if member not in archive.namelist():
                continue
            root = ET.fromstring(archive.read(member))
            if member.endswith(("footnotes.xml", "endnotes.xml")):
                for note in root:
                    note_id = note.get(f"{W}id")
                    if note_id is not None and int(note_id) <= 0:
                        continue
                    parts.append("".join(node.text or "" for node in note.iter(f"{W}t")))
            else:
                parts.append("".join(node.text or "" for node in root.iter(f"{W}t")))
    return "\n".join(parts)


def _requested_fonts(path: Path) -> list[str]:
    fonts: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        for member in ("word/styles.xml", "word/document.xml", "word/footnotes.xml"):
            if member not in archive.namelist():
                continue
            root = ET.fromstring(archive.read(member))
            for node in root.iter(f"{W}rFonts"):
                for name in ("ascii", "hAnsi", "eastAsia", "cs"):
                    value = node.get(f"{W}{name}")
                    if value and not value.startswith("+"):
                        fonts.add(value)
    return sorted(fonts)


def _font_resolution(fonts: list[str]) -> dict[str, str]:
    matcher = shutil.which("fc-match")
    if matcher is None:
        return {}
    resolved: dict[str, str] = {}
    for font in fonts:
        completed = subprocess.run(
            [matcher, "--format=%{family}", font],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        resolved[font] = completed.stdout.strip()[:300]
    return resolved


def _interleaved_bibliography_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Find rendered blocks that likely contain two bibliography entries.

    A common failed two-column reconstruction concatenates the left and right
    columns into one DOCX paragraph.  Ordinary page-density thresholds can miss
    it because the paragraph still wraps inside the margins.  The strongest
    language-independent Latin-script signals are a new ``Surname, Given``
    token after a completed publication year, or a second author token after a
    colon in a block that already begins with an author.  Coordinated authors
    (``..., and Surname, Given``) do not match either boundary.

    The caller deliberately requires multiple suspicious blocks on one page
    before failing the release; a single match remains an audit warning.
    """

    suspicious: list[dict[str, Any]] = []
    for block_index, block in enumerate(blocks):
        if len(block) < 5:
            continue
        text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
        if not text:
            continue
        after_year = list(_BIBLIOGRAPHY_AUTHOR_AFTER_ENTRY_END.finditer(text))
        after_colon = (
            [
                match
                for match in _BIBLIOGRAPHY_AUTHOR_AFTER_COLON.finditer(text)
                if re.match(
                    r"\s*,\s*(?:(?:and|or)\b|&)",
                    text[match.end() :],
                    flags=re.I,
                )
                is None
            ]
            if _BIBLIOGRAPHY_AUTHOR_AT_START.match(text)
            else []
        )
        if not after_year and not after_colon:
            continue
        suspicious.append(
            {
                "block_index": block_index,
                "bbox": [round(float(value), 2) for value in block[:4]],
                "year_boundary_authors": [
                    match.group("author") for match in after_year[:3]
                ],
                "colon_boundary_authors": [
                    match.group("author") for match in after_colon[:3]
                ],
                "text": text[:300],
            }
        )
    return suspicious


def _index_entry_label_between(value: str) -> bool:
    """Return whether text between aliases looks like another entry label."""

    without_references = _INDEX_REFERENCE_RUN.sub(" ", value)
    canonical = re.sub(r"[^A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", " ", without_references)
    if len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", canonical)) >= 2:
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]{1,40}", canonical)
    return any(len(word) >= 3 and word[0].isupper() for word in words)


def _looks_like_index_page(blocks: list[Any]) -> bool:
    """Identify index-like pages without relying on a book-specific heading."""

    texts = [
        re.sub(r"\s+", " ", str(block[4] or "")).strip()
        for block in blocks
        if len(block) >= 5
    ]
    alias_reference_count = sum(
        len(_INDEX_ALIAS_WITH_REFERENCES.findall(text)) for text in texts
    )
    reference_run_count = sum(len(_INDEX_REFERENCE_RUN.findall(text)) for text in texts)
    # Requiring both signals keeps ordinary prose, notes, and bibliographies out
    # of this specialised check while allowing index continuation pages that do
    # not repeat an "Index" heading.
    return alias_reference_count >= 6 and reference_run_count >= 8


def _interleaved_index_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Find blocks that likely merge independent index-column entries.

    Correct one-column index output normally yields one logical entry per text
    block, including wrapped page-number lists.  Failed two-column extraction
    instead produces either two entry aliases in one block, separated by a new
    label, or two independent page-reference runs with a new alias between
    them.  This check is enabled only on pages with a high density of index
    signatures.  The caller requires multiple suspicious blocks for a hard
    failure, so uncommon cross-references remain reviewable warnings.
    """

    if not _looks_like_index_page(blocks):
        return []
    suspicious: list[dict[str, Any]] = []
    for block_index, block in enumerate(blocks):
        if len(block) < 5:
            continue
        text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
        aliases = list(_INDEX_ALIAS.finditer(text))
        reference_runs = list(_INDEX_REFERENCE_RUN.finditer(text))
        reasons: list[str] = []

        if len(aliases) >= 2 and reference_runs:
            if any(
                _index_entry_label_between(text[left.end() : right.start()])
                for left, right in zip(aliases, aliases[1:])
            ):
                reasons.append("multiple_entry_aliases")

        if len(reference_runs) >= 2:
            if any(
                _INDEX_ALIAS.search(text[left.end() : right.start()])
                for left, right in zip(reference_runs, reference_runs[1:])
            ):
                reasons.append("independent_reference_runs")

        if not reasons:
            continue
        suspicious.append(
            {
                "block_index": block_index,
                "bbox": [round(float(value), 2) for value in block[:4]],
                "reasons": reasons,
                "alias_count": len(aliases),
                "reference_run_count": len(reference_runs),
                "text": text[:300],
            }
        )
    return suspicious


def _bundled_fontconfig(soffice: str) -> Path | None:
    explicit = os.getenv("DOCX_RENDER_FONTCONFIG", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    executable = Path(soffice).expanduser().resolve()
    for parent in executable.parents:
        candidates.extend(
            (
                parent
                / "native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fontconfig/fonts.conf",
                parent
                / "LibreOfficeDev.app/Contents/Resources/fontconfig/fonts.conf",
                parent / "LibreOffice.app/Contents/Resources/fontconfig/fonts.conf",
            )
        )
    candidates.extend(
        (
            Path(
                "/Applications/LibreOffice.app/Contents/Resources/fontconfig/fonts.conf"
            ),
            Path(
                "/Applications/LibreOfficeDev.app/Contents/Resources/fontconfig/fonts.conf"
            ),
        )
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def inspect_rendered_pdf(pdf_path: Path, *, expected_text: str) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    rendered_text: list[str] = []
    rendered_fonts: set[str] = set()
    cjk_ink_samples: list[float] = []
    with fitz.open(pdf_path) as document:
        if document.page_count < 1:
            issues.append(_issue("docx_render_empty_pdf", "Word 渲染结果没有页面。"))
        for page_number, page in enumerate(document, start=1):
            rendered_fonts.update(
                str(font[3])
                for font in page.get_fonts(full=True)
                if len(font) > 3 and font[3]
            )
            text = page.get_text("text")
            rendered_text.append(text)
            blocks = page.get_text("blocks")
            interleaved_bibliography_blocks = _interleaved_bibliography_blocks(blocks)
            interleaved_index_blocks = _interleaved_index_blocks(blocks)
            text_dictionary = page.get_text("dict")
            drawings = page.get_drawings()
            images = page.get_images(full=True)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            grayscale = image.convert("L")
            for block in text_dictionary.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        span_text = str(span.get("text") or "")
                        cjk_count = len(re.findall(r"[\u3400-\u9fff]", span_text))
                        if cjk_count < 5:
                            continue
                        x0, y0, x1, y1 = (float(value) for value in span["bbox"])
                        crop = grayscale.crop(
                            (
                                max(0, int(x0)),
                                max(0, int(y0)),
                                min(grayscale.width, int(x1 + 1)),
                                min(grayscale.height, int(y1 + 1)),
                            )
                        )
                        ink_pixels = sum(crop.histogram()[:220])
                        cjk_ink_samples.append(ink_pixels / cjk_count)
            difference = ImageChops.difference(
                image,
                Image.new("RGB", image.size, "white"),
            )
            content_box = difference.getbbox()
            is_blank = not text.strip() and not drawings and not images and content_box is None
            text_characters = len(_canonical_characters(text))
            if is_blank:
                issues.append(
                    _issue(
                        "docx_render_blank_page",
                        "Word 渲染产生了意外空白页。",
                        page=page_number,
                    )
                )
            edge_contact = False
            if content_box is not None:
                left, top, right, bottom = content_box
                edge_contact = bool(
                    left <= 1
                    or top <= 1
                    or right >= image.width - 1
                    or bottom >= image.height - 1
                )
            if edge_contact:
                issues.append(
                    _issue(
                        "docx_render_content_at_page_edge",
                        "Word 渲染内容接触页面边缘，可能存在裁切或表格溢出。",
                        page=page_number,
                        content_box=list(content_box or ()),
                        page_pixels=[image.width, image.height],
                    )
                )
            outside_blocks = [
                [round(float(value), 2) for value in block[:4]]
                for block in blocks
                if block[0] < -0.5
                or block[1] < -0.5
                or block[2] > page.rect.width + 0.5
                or block[3] > page.rect.height + 0.5
            ]
            if outside_blocks:
                issues.append(
                    _issue(
                        "docx_render_text_outside_page",
                        "Word 渲染文本框超出页面几何边界。",
                        page=page_number,
                        blocks=outside_blocks[:10],
                    )
                )
            if text_characters > 3200:
                issues.append(
                    _issue(
                        "docx_render_page_too_dense",
                        "Word 单页文字密度异常高，可能发生行距压缩或分页失效。",
                        page=page_number,
                        text_characters=text_characters,
                    )
                )
            elif text_characters > 2200:
                warnings.append(
                    _issue(
                        "docx_render_page_density_warning",
                        "Word 单页文字偏密，建议抽查该页。",
                        page=page_number,
                        text_characters=text_characters,
                    )
                )
            if len(interleaved_bibliography_blocks) >= 2:
                issues.append(
                    _issue(
                        "docx_render_interleaved_bibliography",
                        "Word 页面疑似把多栏书目交错串接为连续段落。",
                        page=page_number,
                        suspicious_block_count=len(interleaved_bibliography_blocks),
                        blocks=interleaved_bibliography_blocks[:6],
                    )
                )
            elif interleaved_bibliography_blocks:
                warnings.append(
                    _issue(
                        "docx_render_interleaved_bibliography_warning",
                        "Word 页面出现疑似被串接的书目条目，建议抽查该页。",
                        page=page_number,
                        suspicious_block_count=1,
                        blocks=interleaved_bibliography_blocks,
                    )
                )
            if len(interleaved_index_blocks) >= 2:
                issues.append(
                    _issue(
                        "docx_render_interleaved_index",
                        "Word 页面疑似把多栏索引交错串接为连续段落。",
                        page=page_number,
                        suspicious_block_count=len(interleaved_index_blocks),
                        blocks=interleaved_index_blocks[:8],
                    )
                )
            elif interleaved_index_blocks:
                warnings.append(
                    _issue(
                        "docx_render_interleaved_index_warning",
                        "Word 页面出现疑似被串接的索引词条，建议抽查该页。",
                        page=page_number,
                        suspicious_block_count=1,
                        blocks=interleaved_index_blocks,
                    )
                )
            sparse = bool(
                not is_blank
                and not drawings
                and not images
                and text_characters < 20
                and content_box is not None
                and (content_box[3] - content_box[1]) < image.height * 0.25
            )
            if sparse:
                warnings.append(
                    _issue(
                        "docx_render_sparse_page_warning",
                        "Word 页面仅含极少文字，可能存在意外分页。",
                        page=page_number,
                        text_characters=text_characters,
                    )
                )
            pages.append(
                {
                    "page": page_number,
                    "text_characters": text_characters,
                    "content_box": list(content_box) if content_box is not None else None,
                    "edge_contact": edge_contact,
                    "blank": is_blank,
                    "sparse": sparse,
                    "interleaved_bibliography_block_count": len(
                        interleaved_bibliography_blocks
                    ),
                    "interleaved_index_block_count": len(interleaved_index_blocks),
                }
            )

    expected = _canonical_characters(expected_text)
    actual = _canonical_characters("\n".join(rendered_text))
    expected_cjk_count = len(re.findall(r"[\u3400-\u9fff]", expected_text))
    median_cjk_ink = (
        statistics.median(cjk_ink_samples) if cjk_ink_samples else 0.0
    )
    if expected_cjk_count >= 100 and (
        len(cjk_ink_samples) < 10 or median_cjk_ink < 8.0
    ):
        issues.append(
            _issue(
                "docx_render_cjk_glyphs_missing",
                "Word 渲染文本层含中文，但页面像素中缺少对应字形。",
                expected_cjk_characters=expected_cjk_count,
                sampled_span_count=len(cjk_ink_samples),
                median_ink_pixels_per_character=round(median_cjk_ink, 3),
            )
        )
    matched_characters = sum((Counter(expected) & Counter(actual)).values())
    coverage = matched_characters / max(1, len(expected))
    if expected and coverage < 0.90:
        issues.append(
            _issue(
                "docx_render_text_coverage_low",
                "Word 渲染后的可复制文本显著少于 DOCX 包中的可见文字。",
                expected_characters=len(expected),
                rendered_characters=len(actual),
                matched_characters=matched_characters,
                coverage=round(coverage, 4),
            )
        )
    elif expected and coverage < 0.97:
        warnings.append(
            _issue(
                "docx_render_text_coverage_warning",
                "Word 渲染文本覆盖率略低，请确认字体替换和特殊字符。",
                expected_characters=len(expected),
                rendered_characters=len(actual),
                matched_characters=matched_characters,
                coverage=round(coverage, 4),
            )
        )
    return {
        "issues": issues,
        "warnings": warnings,
        "metrics": {
            "page_count": len(pages),
            "expected_text_characters": len(expected),
            "rendered_text_characters": len(actual),
            "matched_text_characters": matched_characters,
            "text_coverage": round(coverage, 4),
            "rendered_fonts": sorted(rendered_fonts),
            "expected_cjk_characters": expected_cjk_count,
            "cjk_span_sample_count": len(cjk_ink_samples),
            "median_cjk_ink_pixels_per_character": round(median_cjk_ink, 3),
            "blank_page_count": sum(bool(item["blank"]) for item in pages),
            "edge_contact_page_count": sum(bool(item["edge_contact"]) for item in pages),
            "dense_page_count": sum(
                int(item["text_characters"]) > 3200 for item in pages
            ),
            "interleaved_bibliography_page_count": sum(
                int(item["interleaved_bibliography_block_count"]) >= 2
                for item in pages
            ),
            "interleaved_index_page_count": sum(
                int(item["interleaved_index_block_count"]) >= 2 for item in pages
            ),
            "sparse_page_count": sum(bool(item["sparse"]) for item in pages),
            "pages": pages,
        },
    }


def verify_docx_render(
    docx_path: str | os.PathLike[str],
    *,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    path = Path(docx_path).expanduser().resolve()
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    soffice = find_soffice()
    if soffice is None:
        word_result = _render_docx_with_word(path, timeout_seconds=timeout_seconds)
        if word_result is not None:
            return word_result
        return {
            "status": "failed",
            "issues": [
                _issue(
                    "docx_renderer_missing",
                    "完整 Word 发布要求 LibreOffice/soffice 渲染器，但当前环境未找到。",
                )
            ],
            "warnings": [],
            "metrics": {"path": str(path)},
        }
    if not path.is_file():
        return {
            "status": "failed",
            "issues": [_issue("docx_render_input_missing", "待渲染 DOCX 不存在。")],
            "warnings": [],
            "metrics": {"path": str(path)},
        }

    requested_fonts = _requested_fonts(path)
    resolved_fonts = _font_resolution(requested_fonts)
    fontconfig_path = _bundled_fontconfig(soffice)
    font_fingerprint = hashlib.sha256(
        repr(
            (
                sorted(requested_fonts),
                sorted(resolved_fonts.items()),
                (
                    hashlib.sha256(fontconfig_path.read_bytes()).hexdigest()
                    if fontconfig_path is not None
                    else None
                ),
            )
        ).encode("utf-8")
    ).hexdigest()
    if fontconfig_path is None:
        issues.append(
            _issue(
                "docx_render_fontconfig_missing",
                "未找到固定的 LibreOffice fontconfig 配置，无法保证中文字体环境。",
            )
        )
    for requested, resolved in resolved_fonts.items():
        if not resolved:
            issues.append(
                _issue(
                    "docx_render_font_unresolved",
                    "Word 样式引用的字体无法由 fontconfig 解析。",
                    requested=requested,
                )
            )
        elif requested.casefold() not in {
            item.strip().casefold() for item in resolved.split(",") if item.strip()
        }:
            warnings.append(
                _issue(
                    "docx_render_font_substitution",
                    "LibreOffice 将 Word 请求字体映射为替代字体。",
                    requested=requested,
                    resolved=resolved,
                )
            )

    with tempfile.TemporaryDirectory(prefix="docx-render-gate-") as directory:
        root = Path(directory)
        output_dir = root / "output"
        profile_dir = root / "profile"
        output_dir.mkdir()
        profile_dir.mkdir()
        profile_uri = profile_dir.resolve().as_uri()
        environment = os.environ.copy()
        environment["HOME"] = str(profile_dir)
        if fontconfig_path is not None:
            environment["FONTCONFIG_FILE"] = str(fontconfig_path)
            environment["FONTCONFIG_PATH"] = str(fontconfig_path.parent)
        if Path("/private/tmp").is_dir():
            environment["TMPDIR"] = "/private/tmp"
        command = [
            soffice,
            "--headless",
            "--norestore",
            "--nodefault",
            "--nolockcheck",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=environment,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "failed",
                "issues": issues
                + [_issue("docx_render_failed", f"{type(exc).__name__}: {exc}")],
                "warnings": warnings,
                "metrics": {
                    "path": str(path),
                    "renderer": soffice,
                    "font_fingerprint": font_fingerprint,
                    "fontconfig": str(fontconfig_path) if fontconfig_path else None,
                    "requested_fonts": requested_fonts,
                    "fonts": resolved_fonts,
                },
            }
        pdf_path = output_dir / f"{path.stem}.pdf"
        if completed.returncode != 0 or not pdf_path.is_file():
            return {
                "status": "failed",
                "issues": issues
                + [
                    _issue(
                        "docx_render_failed",
                        "LibreOffice 未生成有效 PDF。",
                        returncode=completed.returncode,
                        stderr=completed.stderr.strip()[-1200:],
                    )
                ],
                "warnings": warnings,
                "metrics": {
                    "path": str(path),
                    "renderer": soffice,
                    "font_fingerprint": font_fingerprint,
                    "fontconfig": str(fontconfig_path) if fontconfig_path else None,
                    "requested_fonts": requested_fonts,
                    "fonts": resolved_fonts,
                },
            }
        inspected = inspect_rendered_pdf(
            pdf_path,
            expected_text=_docx_visible_text(path),
        )
        issues.extend(inspected["issues"])
        warnings.extend(inspected["warnings"])
        metrics = {
            "path": str(path),
            "renderer": soffice,
            "font_fingerprint": font_fingerprint,
            "fontconfig": str(fontconfig_path) if fontconfig_path else None,
            "requested_fonts": requested_fonts,
            "fonts": resolved_fonts,
            **inspected["metrics"],
        }
    return {
        "status": "failed" if issues else "passed",
        "issues": issues,
        "warnings": warnings,
        "metrics": metrics,
    }
