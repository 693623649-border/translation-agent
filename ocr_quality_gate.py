"""Small, deterministic checks shared by OCR review and translation."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any
from pathlib import Path


def punctuation_signature(text: str) -> tuple[str, ...]:
    """Return source punctuation in order, preserving its exact glyphs."""

    return tuple(
        character
        for character in text
        if unicodedata.category(character).startswith("P")
    )



_QUOTE_PAIRS = {"「": "」", "『": "』", "〈": "〉", "《": "》", "（": "）", "(": ")", "【": "】"}


def quotation_signature(text: str) -> tuple[str, ...]:
    """Preserve quote/bracket glyphs and nesting while allowing natural punctuation."""
    return tuple(c for c in text if c in "「」『』〈〉《》")


def ocr_structure_anomalies(pages: Mapping[int, str]) -> dict[int, list[str]]:
    """Conservative document-level signals, not proof of transcription accuracy.

    Carry the stack across pages: a correctly continued quotation is valid.
    Both missing delimiters are invisible to this check and require image review.
    Callers remove image-grounded running heads through scan_layout first;
    repeated prose is never suppressed here. Unresolved opens implicate their page.
    """
    result: dict[int, list[str]] = {}
    stack: list[tuple[str, int]] = []
    def flag(page: int, reason: str) -> None:
        if reason not in result.setdefault(page, []):
            result[page].append(reason)
    for page, text in sorted(pages.items()):
        if re.search(r"(?m)^\s*へ\s*$", text):
            flag(page, "isolated_kana_he")
        if re.search(r"[<>＜＞]", text):
            flag(page, "angle_bracket_noise")
        for char in text:
            if char in _QUOTE_PAIRS:
                stack.append((char, page))
            elif char in _QUOTE_PAIRS.values():
                if stack and _QUOTE_PAIRS[stack[-1][0]] == char:
                    stack.pop()
                else:
                    flag(page, "unmatched_closing_quote")
    for _char, page in stack:
        flag(page, "unclosed_document_quote")
    return result

def normalize_ocr_text(text: str) -> str:
    return "".join(character for character in text if not character.isspace())


def character_error_rate(left: str, right: str) -> float:
    """Whitespace-insensitive character error rate with O(min(n, m)) memory."""

    source = normalize_ocr_text(left)
    target = normalize_ocr_text(right)
    if not source:
        return 0.0 if not target else 1.0
    if len(source) < len(target):
        source, target = target, source
    previous = list(range(len(target) + 1))
    for row, source_char in enumerate(source, 1):
        current = [row]
        for column, target_char in enumerate(target, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (source_char != target_char),
                )
            )
        previous = current
    return previous[-1] / len(source)


def comparison_reasons(
    primary: str,
    fallback: str,
    *,
    cer_threshold: float | None,
    hard_failure: bool = False,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if hard_failure:
        reasons.append("primary_hard_failure")
    if punctuation_signature(primary) != punctuation_signature(fallback):
        reasons.append("punctuation_difference")
    cer = character_error_rate(primary, fallback)
    if cer_threshold is None:
        if normalize_ocr_text(primary) != normalize_ocr_text(fallback):
            reasons.append("uncalibrated_model_difference")
    elif cer > cer_threshold:
        reasons.append("model_cer_difference")
    return tuple(reasons)


def review_sample_pages(
    pages: Iterable[int], *, source_sha256: str, already_reviewed: Iterable[int] = ()
) -> list[int]:
    """Choose a stable random full-page sample from the remaining body pages."""

    unreviewed = sorted(set(pages) - set(already_reviewed))
    if not unreviewed:
        return []
    sample_count = min(10, len(unreviewed), max(3, math.ceil(len(unreviewed) / 20)))
    seed = int(hashlib.sha256(source_sha256.encode("ascii", errors="ignore")).hexdigest(), 16)
    return sorted(random.Random(seed).sample(unreviewed, sample_count))


def calibrated_confidence_threshold(
    human_labels: Iterable[Mapping[str, Any]], *, minimum_pages: int = 10
) -> float | None:
    """Conservatively flag confidence at or below the weakest human-found error.

    Fewer than ten human labels, malformed data, or an all-correct sample does
    not create a threshold. A caller must then leave low-confidence fallback
    disabled; hard failures remain eligible for the one-time fallback.
    """

    labels = list(human_labels)
    if len(labels) < minimum_pages:
        return None
    error_scores: list[float] = []
    for item in labels:
        score = item.get("confidence")
        correct = item.get("matches_image")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
            or not isinstance(correct, bool)
        ):
            return None
        if not correct:
            error_scores.append(float(score))
    return max(error_scores) if error_scores else None


def calibrated_disagreement_threshold(
    human_labels: Iterable[Mapping[str, Any]], *, minimum_pages: int = 10
) -> float | None:
    """Find a CER boundary only when reviewed pages separate clean and bad OCR."""

    rows = list(human_labels)
    if len(rows) < minimum_pages:
        return None
    correct: list[float] = []
    incorrect: list[float] = []
    for item in rows:
        rate = item.get("model_cer")
        matches_image = item.get("primary_matches_image")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not 0 <= rate <= 1
            or not isinstance(matches_image, bool)
        ):
            return None
        (correct if matches_image else incorrect).append(float(rate))
    if not correct or not incorrect or max(correct) >= min(incorrect):
        return None
    return (max(correct) + min(incorrect)) / 2


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_ocr_notes(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{random.getrandbits(64):016x}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
