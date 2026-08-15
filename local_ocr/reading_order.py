from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TextLine:
    text: str
    score: float
    polygon: tuple[tuple[float, float], ...]

    @property
    def left(self) -> float:
        return min(point[0] for point in self.polygon)

    @property
    def right(self) -> float:
        return max(point[0] for point in self.polygon)

    @property
    def top(self) -> float:
        return min(point[1] for point in self.polygon)

    @property
    def bottom(self) -> float:
        return max(point[1] for point in self.polygon)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


def _row_order(lines: Sequence[TextLine]) -> list[TextLine]:
    """Cluster small y variations into visual rows, then order left-to-right."""

    ordered = sorted(lines, key=lambda line: (line.center_y, line.left))
    if not ordered:
        return []
    tolerance = max(2.0, statistics.median(line.height for line in ordered) * 0.55)
    rows: list[list[TextLine]] = []
    row_centers: list[float] = []
    for line in ordered:
        if rows and abs(line.center_y - row_centers[-1]) <= tolerance:
            rows[-1].append(line)
            row_centers[-1] = statistics.mean(item.center_y for item in rows[-1])
        else:
            rows.append([line])
            row_centers.append(line.center_y)
    return [
        line
        for row in rows
        for line in sorted(row, key=lambda item: item.left)
    ]


_CJK_OR_KANA = re.compile(
    "["
    "\\u3400-\\u4dbf"
    "\\u4e00-\\u9fff"
    "\\uf900-\\ufaff"
    "\\u3040-\\u30ff"
    "\\u31f0-\\u31ff"
    "\\uff66-\\uff9f"
    "\\U00020000-\\U0002fa1f"
    "]"
)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def _merge_edge_vertical_fragments(lines: Sequence[TextLine]) -> list[TextLine]:
    """Reassemble vertically fragmented running heads on horizontal pages.

    Paddle sometimes detects every glyph of a vertical running head as a
    separate line.  Feeding those boxes directly to row ordering interleaves
    the title one glyph at a time with the body.  Only narrow columns outside
    robust body bounds are eligible here; this deliberately leaves lists,
    source code, and isolated glyphs inside the text block untouched.
    """

    if len(lines) < 4:
        return list(lines)

    # Long, horizontal detections give us a conservative estimate of the main
    # text block.  Short headings, marginal furniture, and fragmented vertical
    # text cannot become anchors.
    anchors = [
        item
        for item in lines
        if (item.right - item.left) >= item.height * 3.0
        and len(item.text.strip()) >= 4
    ]
    if len(anchors) < 2:
        return list(lines)
    median_anchor_width = statistics.median(
        item.right - item.left for item in anchors
    )
    main_anchors = [
        item
        for item in anchors
        if (item.right - item.left) >= median_anchor_width * 0.55
    ]
    if len(main_anchors) < 2:
        return list(lines)

    typical_height = statistics.median(item.height for item in main_anchors)
    body_left = _percentile([item.left for item in main_anchors], 0.10)
    body_right = _percentile([item.right for item in main_anchors], 0.90)
    max_fragment_width = max(3.0, typical_height * 2.2)

    candidates: dict[str, list[TextLine]] = {"left": [], "right": []}
    for item in lines:
        width = max(1.0, item.right - item.left)
        text = item.text.strip()
        if (
            not text
            or len(text) > 4
            or not all(_CJK_OR_KANA.fullmatch(character) for character in text)
        ):
            continue
        if width > max_fragment_width or width > item.height * 1.25:
            continue
        if item.right < body_left:
            candidates["left"].append(item)
        elif item.left > body_right:
            candidates["right"].append(item)

    merged_groups: list[tuple[list[TextLine], TextLine]] = []
    for side_values in candidates.values():
        if len(side_values) < 2:
            continue
        widths = [max(1.0, item.right - item.left) for item in side_values]
        x_tolerance = max(2.0, typical_height * 0.7, statistics.median(widths) * 0.75)

        columns: list[list[TextLine]] = []
        centers: list[float] = []
        for item in sorted(side_values, key=lambda value: value.center_x):
            best_index: int | None = None
            best_distance = float("inf")
            for index, center in enumerate(centers):
                distance = abs(item.center_x - center)
                if distance <= x_tolerance and distance < best_distance:
                    best_index = index
                    best_distance = distance
            if best_index is None:
                columns.append([item])
                centers.append(item.center_x)
            else:
                columns[best_index].append(item)
                centers[best_index] = statistics.mean(
                    value.center_x for value in columns[best_index]
                )

        for column in columns:
            ordered = sorted(column, key=lambda value: value.center_y)
            median_fragment_height = statistics.median(item.height for item in ordered)
            max_gap = max(3.0, typical_height * 4.0, median_fragment_height * 3.0)
            runs: list[list[TextLine]] = []
            for item in ordered:
                if runs and item.top - runs[-1][-1].bottom <= max_gap:
                    runs[-1].append(item)
                else:
                    runs.append([item])

            for run in runs:
                if len(run) < 2:
                    continue
                text = "".join(item.text.strip() for item in run)
                if len(_CJK_OR_KANA.findall(text)) < 3:
                    continue
                left = min(item.left for item in run)
                right = max(item.right for item in run)
                top = min(item.top for item in run)
                bottom = max(item.bottom for item in run)
                if bottom - top < max(3.0, (right - left) * 2.5):
                    continue
                merged = TextLine(
                    text=text,
                    score=statistics.mean(item.score for item in run),
                    polygon=((left, top), (right, top), (right, bottom), (left, bottom)),
                )
                merged_groups.append((run, merged))

    if not merged_groups:
        return list(lines)
    consumed = {id(item) for run, _ in merged_groups for item in run}
    return [item for item in lines if id(item) not in consumed] + [
        merged for _, merged in merged_groups
    ]


def order_text_lines(
    lines: Iterable[TextLine],
    *,
    reading_direction: str,
    horizontal_columns: int = 1,
) -> list[TextLine]:
    """Return deterministic book-page reading order.

    Horizontal multi-column mode is explicit and therefore fail-safe: callers
    opt into two or more columns only for pages/books known to use them.
    Vertical Japanese is ordered right-to-left by column and top-to-bottom
    inside each column.
    """

    values = [line for line in lines if line.text.strip()]
    if reading_direction == "vertical":
        if not values:
            return []
        widths = [max(1.0, line.right - line.left) for line in values]
        tolerance = max(2.0, statistics.median(widths) * 0.65)
        columns: list[list[TextLine]] = []
        column_centers: list[float] = []
        for line in sorted(values, key=lambda item: -item.center_x):
            if columns and abs(line.center_x - column_centers[-1]) <= tolerance:
                columns[-1].append(line)
                column_centers[-1] = statistics.mean(
                    item.center_x for item in columns[-1]
                )
            else:
                columns.append([line])
                column_centers.append(line.center_x)
        return [
            line
            for column in columns
            for line in sorted(column, key=lambda item: item.center_y)
        ]
    if reading_direction != "horizontal":
        raise ValueError("reading_direction must be horizontal or vertical")
    values = _merge_edge_vertical_fragments(values)
    columns = max(1, int(horizontal_columns))
    if columns == 1 or len(values) < columns * 2:
        return _row_order(values)

    minimum = min(line.left for line in values)
    maximum = max(line.right for line in values)
    width = max(1.0, maximum - minimum)
    # Full-width headings and page furniture are kept in global y order.  The
    # remaining lines are placed into explicit equal-width book columns.
    spanning = [line for line in values if (line.right - line.left) >= width * 0.72]
    regular = [line for line in values if line not in spanning]
    groups: list[list[TextLine]] = [[] for _ in range(columns)]
    for line in regular:
        index = min(
            columns - 1,
            max(0, int(((line.center_x - minimum) / width) * columns)),
        )
        groups[index].append(line)

    top_spanning = [
        line
        for line in spanning
        if not regular or line.center_y <= min(item.center_y for item in regular)
    ]
    bottom_spanning = [line for line in spanning if line not in top_spanning]
    return [
        *_row_order(top_spanning),
        *(line for group in groups for line in _row_order(group)),
        *_row_order(bottom_spanning),
    ]
