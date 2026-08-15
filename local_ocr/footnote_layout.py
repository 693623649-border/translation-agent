from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


MAX_FOOTNOTE_LABEL = 100
_BRACKETED_MARKER = re.compile(r"(?:〔|\[)\s*(\d{1,3})\s*(?:〕|\])")
_PLAIN_LEADING_MARKER = re.compile(r"^(\d{1,3})(?=\s|$)")
_PAGE_NUMBER = re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$")


def circled_number(character: str) -> int | None:
    """Return the numeric value of Unicode circled 1..50."""

    if len(character) != 1:
        return None
    codepoint = ord(character)
    if 0x2460 <= codepoint <= 0x2473:
        return codepoint - 0x245F
    if 0x3251 <= codepoint <= 0x325F:
        return codepoint - 0x323C
    if 0x32B1 <= codepoint <= 0x32BF:
        return codepoint - 0x328D
    return None


def marker_numbers(text: str) -> tuple[int, ...]:
    """Extract explicit note markers without inferring a missing number."""

    values: list[int] = []
    for character in text:
        value = circled_number(character)
        if value is not None:
            values.append(value)
    values.extend(
        value
        for match in _BRACKETED_MARKER.finditer(text)
        for value in (int(match.group(1)),)
        if 1 <= value <= MAX_FOOTNOTE_LABEL
    )
    return tuple(values)


def leading_marker_number(text: str) -> int | None:
    """Return a marker only when it is the first visible token of a line."""

    stripped = text.lstrip()
    if not stripped:
        return None
    value = circled_number(stripped[0])
    if value is not None:
        return value
    match = _BRACKETED_MARKER.match(stripped)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= MAX_FOOTNOTE_LABEL else None


def leading_footer_marker_number(text: str) -> int | None:
    """Read a label inside an already-proven footer marker column.

    Paddle sometimes drops the circle but retains its one- or two-digit
    content. Plain digits are accepted only through this geometry-scoped API;
    they remain forbidden in whole-page/body marker extraction.
    """

    explicit = leading_marker_number(text)
    if explicit is not None:
        return explicit
    match = _PLAIN_LEADING_MARKER.match(text.lstrip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= MAX_FOOTNOTE_LABEL else None


@dataclass(frozen=True)
class LayoutLine:
    text: str
    score: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class FooterRegion:
    boundary_y: float
    gap_top: float
    gap_bottom: float
    body_median_height: float
    footer_median_height: float
    confidence: float
    lines: tuple[LayoutLine, ...]
    definition_numbers: tuple[int, ...]
    body_marker_numbers: tuple[int, ...]


@dataclass(frozen=True)
class FooterSeparator:
    top: int
    bottom: int
    left: int
    right: int
    density: float
    bridged: bool

    @property
    def y(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def width(self) -> int:
        return self.right - self.left + 1


@dataclass(frozen=True)
class InlineCircleCandidate:
    left: int
    top: int
    right: int
    bottom: int
    score: float

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0


def parse_layout_lines(
    metadata: Mapping[str, Any],
    *,
    page_width: float,
    page_height: float,
) -> tuple[LayoutLine, ...]:
    """Validate the bounded Paddle metadata before geometry is trusted."""

    if metadata.get("layout_lines_truncated"):
        raise ValueError("Paddle layout metadata is truncated")
    raw_lines = metadata.get("layout_lines")
    if not isinstance(raw_lines, list):
        raise ValueError("Paddle response has no layout_lines metadata")
    declared = metadata.get("layout_line_count")
    if declared != len(raw_lines):
        raise ValueError("Paddle layout line count is inconsistent")

    parsed: list[LayoutLine] = []
    for raw in raw_lines:
        if not isinstance(raw, Mapping):
            raise ValueError("Paddle layout line is not an object")
        bbox = raw.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("Paddle layout line has no four-value bbox")
        try:
            left, top, right, bottom = (float(value) for value in bbox)
            score = float(raw.get("score") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Paddle layout line geometry is not numeric") from exc
        if not all(math.isfinite(value) for value in (left, top, right, bottom, score)):
            raise ValueError("Paddle layout line geometry is not finite")
        if right <= left or bottom <= top:
            raise ValueError("Paddle layout line bbox is empty")
        # A small detector overshoot is harmless, but wildly out-of-page boxes
        # indicate that this metadata does not describe the supplied image.
        tolerance_x = max(2.0, page_width * 0.02)
        tolerance_y = max(2.0, page_height * 0.02)
        if (
            left < -tolerance_x
            or top < -tolerance_y
            or right > page_width + tolerance_x
            or bottom > page_height + tolerance_y
        ):
            raise ValueError("Paddle layout line lies outside the page")
        text = str(raw.get("text") or "").strip()
        if text:
            parsed.append(
                LayoutLine(
                    text=text,
                    score=score,
                    left=max(0.0, left),
                    top=max(0.0, top),
                    right=min(page_width, right),
                    bottom=min(page_height, bottom),
                )
            )
    return tuple(sorted(parsed, key=lambda line: (line.top, line.left)))


@dataclass(frozen=True)
class _VisualRow:
    lines: tuple[LayoutLine, ...]

    @property
    def top(self) -> float:
        return min(line.top for line in self.lines)

    @property
    def bottom(self) -> float:
        return max(line.bottom for line in self.lines)

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)


def _visual_rows(lines: Sequence[LayoutLine]) -> tuple[_VisualRow, ...]:
    if not lines:
        return ()
    tolerance = max(2.0, statistics.median(line.height for line in lines) * 0.45)
    groups: list[list[LayoutLine]] = []
    centers: list[float] = []
    for line in sorted(lines, key=lambda item: (item.center_y, item.left)):
        best: int | None = None
        distance = float("inf")
        for index, center in enumerate(centers):
            current = abs(line.center_y - center)
            if current <= tolerance and current < distance:
                best = index
                distance = current
        if best is None:
            groups.append([line])
            centers.append(line.center_y)
        else:
            groups[best].append(line)
            centers[best] = statistics.mean(item.center_y for item in groups[best])
    return tuple(
        _VisualRow(tuple(sorted(group, key=lambda item: item.left)))
        for group in sorted(groups, key=lambda group: min(item.top for item in group))
    )


def _is_page_number(line: LayoutLine, page_width: float, page_height: float) -> bool:
    return bool(
        line.top >= page_height * 0.80
        and line.width <= page_width * 0.14
        and _PAGE_NUMBER.fullmatch(line.text)
    )


def _deduplicate(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(values))


def detect_footer_separators(grayscale: Any) -> tuple[FooterSeparator, ...]:
    """Detect thin, isolated horizontal rules in the lower-left page area.

    The thresholds are scale-relative and follow the print geometry rather
    than OCR text.  A conservative bridge admits a degraded/dashed scan only
    when the original black-pixel density remains high enough.  Text strokes
    are rejected by checking for vertical ink immediately above and below.
    """

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Paddle installations have numpy.
        raise RuntimeError("numpy is required for separator detection") from exc

    values = np.asarray(grayscale)
    if values.ndim != 2 or values.shape[0] < 32 or values.shape[1] < 32:
        raise ValueError("separator detection requires a two-dimensional grayscale page")
    height, width = (int(values.shape[0]), int(values.shape[1]))
    ink = values < 175
    x_min, x_max = round(width * 0.04), round(width * 0.62)
    y_min, y_max = round(height * 0.25), round(height * 0.93)
    minimum_exact = max(24, round(width * 0.038))
    minimum_bridged = max(60, round(width * 0.095))
    maximum_gap = max(5, round(width * 0.016))
    merge_rows = max(3, round(height * 0.0025))

    row_candidates: list[tuple[int, int, int, float, bool]] = []
    for y in range(y_min, y_max):
        xs = np.flatnonzero(ink[y, x_min:x_max])
        if not len(xs):
            continue
        xs = xs + x_min
        segments: list[tuple[int, int]] = []
        start = int(xs[0])
        previous = start
        for raw_x in xs[1:]:
            x = int(raw_x)
            if x > previous + 1:
                segments.append((start, previous))
                start = x
            previous = x
        segments.append((start, previous))

        exact_choices: list[tuple[int, int, float, bool]] = []
        for start, end in segments:
            span = end - start + 1
            if span >= minimum_exact:
                exact_choices.append((start, end, 1.0, False))

        bridged_choices: list[tuple[int, int, float, bool]] = []
        group_start, group_end = segments[0]
        group_black = group_end - group_start + 1
        for start, end in segments[1:] + [(x_max + maximum_gap + 1, x_max)]:
            if start - group_end - 1 <= maximum_gap:
                group_end = end
                group_black += end - start + 1
                continue
            span = group_end - group_start + 1
            density = group_black / max(1, span)
            if span >= minimum_bridged and density >= 0.18:
                bridged_choices.append((group_start, group_end, density, True))
            group_start, group_end = start, end
            group_black = max(0, end - start + 1)
        # A true contiguous rule is substantially safer than a bridged text
        # row. Use bridging only when the row has no qualifying exact run.
        choices = exact_choices or bridged_choices
        if choices:
            start, end, density, bridged = max(
                choices,
                key=lambda item: ((item[1] - item[0] + 1) * item[2], not item[3]),
            )
            row_candidates.append((y, start, end, density, bridged))

    clusters: list[list[tuple[int, int, int, float, bool]]] = []
    for candidate in row_candidates:
        if clusters and candidate[0] - clusters[-1][-1][0] <= merge_rows:
            clusters[-1].append(candidate)
        else:
            clusters.append([candidate])

    separators: list[FooterSeparator] = []
    clearance = max(3, round(height * 0.0035))
    for cluster in clusters:
        exact_cluster = [item for item in cluster if not item[4]]
        best = max(
            exact_cluster or cluster,
            key=lambda item: ((item[2] - item[1] + 1) * item[3], not item[4]),
        )
        _, left, right, density, bridged = best
        top, bottom = cluster[0][0], cluster[-1][0]
        # Printed rules have blank paper on both sides. Horizontal strokes in
        # glyphs retain many vertical neighbors and fail this isolation gate.
        above = ink[max(0, top - clearance * 2) : max(0, top - clearance), left : right + 1]
        below = ink[
            min(height, bottom + clearance + 1) : min(height, bottom + clearance * 2 + 1),
            left : right + 1,
        ]
        above_occupancy = (
            float(np.any(above, axis=0).mean()) if above.size else 0.0
        )
        below_occupancy = (
            float(np.any(below, axis=0).mean()) if below.size else 0.0
        )
        if max(above_occupancy, below_occupancy) > 0.28:
            continue
        if bottom - top + 1 > max(12, round(height * 0.012)):
            continue
        separators.append(
            FooterSeparator(
                top=top,
                bottom=bottom,
                left=left,
                right=right,
                density=density,
                bridged=bridged,
            )
        )
    return tuple(separators)


def detect_inline_circle_candidates(
    grayscale: Any,
    lines: Sequence[LayoutLine],
    *,
    boundary_y: float,
    max_candidates: int = 64,
) -> tuple[InlineCircleCandidate, ...]:
    """Return image-space ring candidates inside Paddle body-line polygons.

    This is intentionally a locator, not a recognizer. It samples circular
    perimeter evidence at several radii around each text-line baseline and
    returns scored boxes. Labels must come from explicit OCR or an external
    reviewed sequence; this function never assigns a footnote number.
    """

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("numpy is required for circle candidate detection") from exc
    values = np.asarray(grayscale)
    if values.ndim != 2:
        raise ValueError("circle detection requires a grayscale page")
    height, width = (int(values.shape[0]), int(values.shape[1]))
    ink = values < 165
    body_lines = [
        line
        for line in lines
        if line.bottom < boundary_y
        and line.top >= height * 0.04
        and line.height >= 8
        and line.width >= width * 0.04
    ]
    raw: list[InlineCircleCandidate] = []
    angles = [index * math.pi / 8.0 for index in range(16)]
    for line in body_lines:
        line_height = line.height
        radii = sorted(
            {
                max(4, round(line_height * ratio))
                # Printed circled references are commonly superscript-sized;
                # Paddle's containing line box can be 5–6x taller than the
                # circle itself. Larger ratios retain ordinary inline circles.
                for ratio in (0.16, 0.20, 0.24, 0.28, 0.32)
            }
        )
        x0 = max(1, round(line.left - line_height * 0.4))
        x1 = min(width - 2, round(line.right + line_height * 0.4))
        # Markers are often aligned to the cap/superscript zone rather than
        # the visual line centre, so scan the interior of the complete line.
        y_values = range(
            max(1, round(line.top + line_height * 0.12)),
            min(height - 1, round(line.bottom - line_height * 0.12)) + 1,
            max(1, round(line_height * 0.08)),
        )
        for radius in radii:
            if x1 - x0 <= radius * 2:
                continue
            xs = np.arange(x0 + radius, x1 - radius + 1, dtype=np.int32)
            if not len(xs):
                continue
            offsets = [
                (round(math.sin(angle) * radius), round(math.cos(angle) * radius))
                for angle in angles
            ]
            for center_y in y_values:
                if center_y - radius < 0 or center_y + radius >= height:
                    continue
                samples = np.stack(
                    [ink[center_y + dy, xs + dx] for dy, dx in offsets],
                    axis=0,
                )
                ring_score = samples.mean(axis=0)
                quadrant_support = np.stack(
                    [
                        samples[0:4].max(axis=0),
                        samples[4:8].max(axis=0),
                        samples[8:12].max(axis=0),
                        samples[12:16].max(axis=0),
                    ],
                    axis=0,
                ).mean(axis=0)
                corner_offset = max(2, round(radius * 0.72))
                corners = np.stack(
                    [
                        ink[center_y - corner_offset, xs - corner_offset],
                        ink[center_y - corner_offset, xs + corner_offset],
                        ink[center_y + corner_offset, xs - corner_offset],
                        ink[center_y + corner_offset, xs + corner_offset],
                    ],
                    axis=0,
                )
                corner_white = 1.0 - corners.mean(axis=0)
                score = ring_score * 0.65 + quadrant_support * 0.20 + corner_white * 0.15
                indices = np.flatnonzero((ring_score >= 0.38) & (quadrant_support >= 0.75))
                for index in indices:
                    current = float(score[index])
                    if current < 0.50:
                        continue
                    center_x = int(xs[index])
                    raw.append(
                        InlineCircleCandidate(
                            left=center_x - radius,
                            top=center_y - radius,
                            right=center_x + radius,
                            bottom=center_y + radius,
                            score=current,
                        )
                    )

    selected: list[InlineCircleCandidate] = []
    for candidate in sorted(raw, key=lambda item: item.score, reverse=True):
        radius = max(1.0, (candidate.right - candidate.left) / 2.0)
        if any(
            abs(candidate.center_x - prior.center_x) <= radius * 0.8
            and abs(candidate.center_y - prior.center_y) <= radius * 0.8
            for prior in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= max_candidates:
            break
    return tuple(sorted(selected, key=lambda item: (item.center_y, item.center_x)))


def footer_region_from_separator(
    lines: Sequence[LayoutLine],
    separator: FooterSeparator,
    *,
    page_width: float,
    page_height: float,
    allow_empty_footer: bool = False,
) -> FooterRegion | None:
    """Map a proven printed separator to Paddle body/footer polygons."""

    margin = max(2.0, page_height * 0.002)
    body_lines = tuple(
        line
        for line in lines
        if line.bottom <= separator.top - margin
        and not _is_page_number(line, page_width, page_height)
    )
    footer_lines = tuple(
        line
        for line in lines
        if line.top >= separator.bottom + margin
        and line.bottom <= page_height * 0.97
        and not _is_page_number(line, page_width, page_height)
    )
    body_samples = [
        line.height
        for line in body_lines
        if page_height * 0.08 <= line.center_y <= page_height * 0.86
        and line.width >= page_width * 0.12
    ]
    visible_chars = sum(len(re.sub(r"\s+", "", line.text)) for line in footer_lines)
    if len(body_samples) < 3:
        return None
    if (not footer_lines or visible_chars < 4) and not allow_empty_footer:
        return None
    body_height = statistics.median(body_samples)
    # When whole-page OCR entirely misses one tiny note line, the printed rule
    # may still trigger a supplemental crop. The estimate is used only for
    # candidate ranking; crop OCR must supply the actual evidence afterwards.
    footer_height = (
        statistics.median(line.height for line in footer_lines)
        if footer_lines
        else body_height * 0.75
    )
    definition_numbers = _deduplicate(
        number
        for line in footer_lines
        for number in (leading_marker_number(line.text),)
        if number is not None
    )
    body_numbers = _deduplicate(
        number
        for line in body_lines
        for number in marker_numbers(line.text)
    )
    paired = set(definition_numbers) & set(body_numbers)
    width_score = min(1.0, separator.width / max(1.0, page_width * 0.12))
    density_score = min(1.0, separator.density / 0.5)
    size_score = min(1.0, max(0.0, 1.0 - footer_height / body_height) * 4.0)
    semantic_score = min(1.0, (len(definition_numbers) + len(paired)) / 2.0)
    confidence = min(
        1.0,
        0.35 * width_score
        + 0.20 * density_score
        + 0.20 * size_score
        + 0.25 * semantic_score,
    )
    return FooterRegion(
        boundary_y=separator.y,
        gap_top=float(separator.top),
        gap_bottom=float(separator.bottom),
        body_median_height=body_height,
        footer_median_height=footer_height,
        confidence=confidence,
        lines=footer_lines,
        definition_numbers=definition_numbers,
        body_marker_numbers=body_numbers,
    )


def detect_footer_region(
    lines: Sequence[LayoutLine],
    *,
    page_width: float,
    page_height: float,
) -> FooterRegion | None:
    """Find a footnote zone using vertical gaps and type-size contrast.

    Text patterns are evaluated only *after* a lower-page geometric partition
    exists.  The function never invents labels, never rewrites text, and
    deliberately returns ``None`` when the page is ambiguous.
    """

    content = tuple(
        line
        for line in lines
        if line.bottom <= page_height * 0.97
        and not _is_page_number(line, page_width, page_height)
    )
    rows = _visual_rows(content)
    if len(rows) < 4:
        return None

    candidates: list[tuple[float, FooterRegion]] = []
    for index in range(1, len(rows) - 1):
        upper = rows[: index + 1]
        lower = rows[index + 1 :]
        gap_top = upper[-1].bottom
        gap_bottom = lower[0].top
        gap = gap_bottom - gap_top
        if not (page_height * 0.45 <= gap_bottom <= page_height * 0.91):
            continue

        upper_lines = tuple(line for row in upper for line in row.lines)
        lower_lines = tuple(line for row in lower for line in row.lines)
        body_samples = [
            line.height
            for line in upper_lines
            if page_height * 0.08 <= line.center_y <= page_height * 0.82
            and line.width >= page_width * 0.12
        ]
        if len(body_samples) < 3:
            continue
        body_height = statistics.median(body_samples)
        footer_height = statistics.median(line.height for line in lower_lines)
        size_ratio = footer_height / max(1.0, body_height)
        definition_numbers = _deduplicate(
            number
            for line in lower_lines
            for number in (leading_marker_number(line.text),)
            if number is not None
        )
        body_numbers = _deduplicate(
            number
            for line in upper_lines
            for number in marker_numbers(line.text)
        )
        paired = set(definition_numbers) & set(body_numbers)
        visible_chars = sum(len(re.sub(r"\s+", "", line.text)) for line in lower_lines)
        substantial_lines = sum(line.width >= page_width * 0.12 for line in lower_lines)

        # A definition label is strong semantic evidence, but still needs a
        # real geometric break.  Missing labels may be proposed only when the
        # lower block is clearly smaller and contains multiple substantial
        # lines.  This keeps running heads and isolated page numbers out.
        labelled = bool(
            definition_numbers
            and gap >= max(2.0, body_height * 0.20)
            and (paired or size_ratio <= 0.90)
        )
        unlabelled_but_geometric = bool(
            not definition_numbers
            and gap >= max(page_height * 0.008, body_height * 0.65)
            and size_ratio <= 0.80
            and substantial_lines >= 2
        )
        if not (labelled or unlabelled_but_geometric):
            continue
        if visible_chars < 8 or (substantial_lines == 0 and not definition_numbers):
            continue

        gap_score = min(2.0, max(0.0, gap / max(1.0, body_height)))
        size_score = min(2.0, max(0.0, (1.0 - size_ratio) * 5.0))
        label_score = min(3.0, float(len(definition_numbers)))
        pair_score = min(3.0, float(len(paired)) * 1.5)
        score = gap_score + size_score + label_score + pair_score
        confidence = min(1.0, score / 8.0)
        candidates.append(
            (
                score,
                FooterRegion(
                    boundary_y=(gap_top + gap_bottom) / 2.0,
                    gap_top=gap_top,
                    gap_bottom=gap_bottom,
                    body_median_height=body_height,
                    footer_median_height=footer_height,
                    confidence=confidence,
                    lines=lower_lines,
                    definition_numbers=definition_numbers,
                    body_marker_numbers=body_numbers,
                ),
            )
        )
    if not candidates:
        return None

    # Prefer the partition supported by the most labels/pairs.  For equal
    # evidence the earlier boundary retains the complete footnote block.
    candidates.sort(
        key=lambda item: (
            item[0],
            len(item[1].definition_numbers),
            len(set(item[1].definition_numbers) & set(item[1].body_marker_numbers)),
            -item[1].boundary_y,
        ),
        reverse=True,
    )
    return candidates[0][1]


__all__ = [
    "FooterSeparator",
    "FooterRegion",
    "InlineCircleCandidate",
    "LayoutLine",
    "MAX_FOOTNOTE_LABEL",
    "circled_number",
    "detect_footer_region",
    "detect_footer_separators",
    "detect_inline_circle_candidates",
    "footer_region_from_separator",
    "leading_footer_marker_number",
    "leading_marker_number",
    "marker_numbers",
    "parse_layout_lines",
]
