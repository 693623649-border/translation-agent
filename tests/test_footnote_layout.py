from __future__ import annotations

import unittest

from local_ocr.footnote_layout import (
    FooterSeparator,
    LayoutLine,
    detect_footer_region,
    detect_footer_separators,
    detect_inline_circle_candidates,
    footer_region_from_separator,
    leading_footer_marker_number,
    leading_marker_number,
    marker_numbers,
    parse_layout_lines,
)


def line(
    text: str,
    top: float,
    bottom: float,
    *,
    left: float = 100,
    right: float = 900,
) -> LayoutLine:
    return LayoutLine(
        text=text,
        score=0.99,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
    )


class MarkerTests(unittest.TestCase):
    def test_explicit_markers_cover_circled_one_to_fifty_and_brackets(self) -> None:
        self.assertEqual(marker_numbers("正文①㉑㊿〔7〕"), (1, 21, 50, 7))
        self.assertEqual(leading_marker_number("  ㉑ 定义"), 21)
        self.assertEqual(leading_marker_number("正文①"), None)

    def test_plain_digit_is_only_accepted_by_footer_scoped_reader(self) -> None:
        self.assertEqual(leading_footer_marker_number("17 脚注定义"), 17)
        self.assertEqual(leading_footer_marker_number("59 脚注定义"), 59)
        self.assertEqual(leading_footer_marker_number("100 脚注定义"), 100)
        self.assertEqual(marker_numbers("正文〔100〕"), (100,))
        self.assertIsNone(leading_marker_number("17 脚注定义"))
        self.assertIsNone(leading_footer_marker_number("1931 年"))
        self.assertIsNone(leading_footer_marker_number("101 脚注定义"))
        self.assertIsNone(leading_footer_marker_number("45-46 页"))
        self.assertIsNone(leading_footer_marker_number("18页"))


class LayoutParsingTests(unittest.TestCase):
    def test_parse_layout_lines_validates_and_orders_boxes(self) -> None:
        metadata = {
            "layout_line_count": 2,
            "layout_lines_truncated": False,
            "layout_lines": [
                {"text": "二", "score": 0.8, "bbox": [10, 30, 90, 40]},
                {"text": "一", "score": 0.9, "bbox": [10, 10, 90, 20]},
            ],
        }
        result = parse_layout_lines(metadata, page_width=100, page_height=100)
        self.assertEqual([item.text for item in result], ["一", "二"])

    def test_parse_layout_lines_fails_closed_when_metadata_was_truncated(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            parse_layout_lines(
                {
                    "layout_line_count": 2,
                    "layout_lines_truncated": True,
                    "layout_lines": [],
                },
                page_width=100,
                page_height=100,
            )


class FooterPartitionTests(unittest.TestCase):
    def _body(self, final_text: str = "末行正文①") -> list[LayoutLine]:
        return [
            line("正文第一行", 100, 124),
            line("正文第二行", 145, 169),
            line("正文第三行", 190, 214),
            line(final_text, 500, 524),
        ]

    def test_detects_smaller_labelled_footer_after_geometric_gap(self) -> None:
        values = self._body() + [
            line("① 第一条脚注", 610, 624),
            line("脚注的续行", 632, 646),
            line("31", 930, 944, left=820, right=850),
        ]
        region = detect_footer_region(values, page_width=1000, page_height=1000)
        self.assertIsNotNone(region)
        assert region is not None
        self.assertEqual(region.definition_numbers, (1,))
        self.assertEqual(region.body_marker_numbers, (1,))
        self.assertGreater(region.boundary_y, 524)
        self.assertLess(region.boundary_y, 610)

    def test_can_flag_unlabelled_small_footer_without_guessing_a_number(self) -> None:
        values = self._body(final_text="末行正文") + [
            line("被漏掉标签的脚注文字", 610, 624),
            line("脚注的续行文字", 632, 646),
        ]
        region = detect_footer_region(values, page_width=1000, page_height=1000)
        self.assertIsNotNone(region)
        assert region is not None
        self.assertEqual(region.definition_numbers, ())
        self.assertEqual(region.body_marker_numbers, ())

    def test_rejects_same_size_circled_list_in_body(self) -> None:
        values = self._body(final_text="正文末行") + [
            line("① 列表项一", 550, 574),
            line("② 列表项二", 595, 619),
        ]
        self.assertIsNone(
            detect_footer_region(values, page_width=1000, page_height=1000)
        )

    def test_printed_separator_accepts_a_single_line_footer(self) -> None:
        values = self._body() + [line("① 单行脚注", 610, 624)]
        region = footer_region_from_separator(
            values,
            FooterSeparator(
                top=570,
                bottom=572,
                left=100,
                right=260,
                density=1.0,
                bridged=False,
            ),
            page_width=1000,
            page_height=1000,
        )
        self.assertIsNotNone(region)
        assert region is not None
        self.assertEqual(region.definition_numbers, (1,))
        self.assertEqual(region.body_marker_numbers, (1,))


class SeparatorImageTests(unittest.TestCase):
    def test_detects_contiguous_and_bounded_bridged_rules(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is unavailable")
        page = np.full((1000, 700), 255, dtype=np.uint8)
        page[600:603, 100:240] = 0
        separators = detect_footer_separators(page)
        self.assertEqual(len(separators), 1)
        self.assertFalse(separators[0].bridged)
        self.assertEqual((separators[0].left, separators[0].right), (100, 239))

        degraded = np.full((1000, 700), 255, dtype=np.uint8)
        degraded[600:603, 100:123] = 0
        degraded[600:603, 131:154] = 0
        degraded[600:603, 162:185] = 0
        degraded[600:603, 193:216] = 0
        separators = detect_footer_separators(degraded)
        self.assertEqual(len(separators), 1)
        self.assertTrue(separators[0].bridged)

    def test_rejects_a_thick_text_like_bar(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is unavailable")
        page = np.full((1000, 700), 255, dtype=np.uint8)
        page[580:620, 100:240] = 0
        self.assertEqual(detect_footer_separators(page), ())

    def test_circle_locator_returns_position_without_assigning_a_label(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is unavailable")
        page = np.full((400, 500), 255, dtype=np.uint8)
        center_x, center_y, radius = 320, 120, 11
        for angle in np.linspace(0, np.pi * 2, 160):
            x = round(center_x + np.cos(angle) * radius)
            y = round(center_y + np.sin(angle) * radius)
            page[y, x] = 0
        candidates = detect_inline_circle_candidates(
            page,
            [line("正文", 100, 140, left=50, right=400)],
            boundary_y=300,
            max_candidates=8,
        )
        self.assertTrue(
            any(
                abs(candidate.center_x - center_x) <= 3
                and abs(candidate.center_y - center_y) <= 3
                for candidate in candidates
            )
        )


if __name__ == "__main__":
    unittest.main()
