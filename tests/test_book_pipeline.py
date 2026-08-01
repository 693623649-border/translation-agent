import json
import re
import sys
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

from book_pipeline import (
    CodingPlanVisionOCR,
    DeepSeekClient,
    GlmClient,
    PageRecord,
    TocEntry,
    apply_page_mapping,
    build_parser,
    build_bookmarked_pdf,
    build_docx,
    build_epub,
    build_translation_client,
    compile_chapters,
    clean_ocr_text,
    clean_translation_text,
    detect_language,
    infer_page_offset,
    import_existing_ocr,
    main,
    markdown_inline_to_plain_text,
    normalize_target_script,
    ocr_pdf,
    parse_page_spec,
    remove_duplicate_title,
    resolve_api_key,
    resolve_translation_api_key,
    resolve_worker_counts,
    save_page_record,
    strip_publication_metadata,
    translate_non_chinese_pages,
    write_json,
)


class UtilityTests(unittest.TestCase):
    def test_write_json_is_safe_for_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.json"
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda value: write_json(path, {"value": value}), range(64)))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(payload["value"], range(64))
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_normalize_target_script_to_simplified_chinese(self) -> None:
        self.assertEqual(
            normalize_target_script("傳統藝術與歷史", "简体中文"),
            "传统艺术与历史",
        )
        self.assertEqual(
            normalize_target_script("傳統藝術與歷史", "繁体中文"),
            "傳統藝術與歷史",
        )
        self.assertEqual(
            normalize_target_script("作者望著天空，阅读名著，成就顯著。", "简体中文"),
            "作者望着天空，阅读名著，成就显著。",
        )

    def test_glm_chat_disables_thinking(self) -> None:
        class RecordingClient(GlmClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key")
                self.requests: list[tuple[str, dict]] = []

            def _post(self, endpoint: str, payload: dict) -> dict:
                self.requests.append((endpoint, payload))
                return {"choices": [{"message": {"content": '{"entries": []}'}}]}

        client = RecordingClient()
        self.assertEqual(client.chat_text("text", system="system"), '{"entries": []}')
        self.assertEqual(client.chat_json("json", system="system"), {"entries": []})
        self.assertEqual(len(client.requests), 2)
        for endpoint, payload in client.requests:
            self.assertEqual(endpoint, "chat/completions")
            self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_deepseek_chat_disables_thinking(self) -> None:
        class RecordingClient(DeepSeekClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", text_model="deepseek-v4-flash")
                self.requests: list[tuple[str, dict]] = []

            def _post(self, endpoint: str, payload: dict) -> dict:
                self.requests.append((endpoint, payload))
                return {"choices": [{"message": {"content": '{"entries": []}'}}]}

        client = RecordingClient()
        self.assertEqual(client.chat_text("text", system="system"), '{"entries": []}')
        self.assertEqual(client.chat_json("json", system="system"), {"entries": []})
        self.assertEqual(len(client.requests), 2)
        for endpoint, payload in client.requests:
            self.assertEqual(endpoint, "chat/completions")
            self.assertEqual(payload["model"], "deepseek-v4-flash")
            self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_provider_keys_are_isolated(self) -> None:
        parser = build_parser()
        with patch.dict(
            "os.environ",
            {"GLM_CODING_API_KEY": "glm-sentinel", "DEEPSEEK_API_KEY": "deepseek-sentinel"},
            clear=True,
        ):
            args = parser.parse_args(["sample.pdf"])
            self.assertEqual(resolve_api_key(args), "glm-sentinel")
            self.assertEqual(resolve_translation_api_key(args), "deepseek-sentinel")

            args = parser.parse_args(
                ["sample.pdf", "--api-key", "glm-cli", "--translation-api-key", "deepseek-cli"]
            )
            self.assertEqual(resolve_api_key(args), "glm-cli")
            self.assertEqual(resolve_translation_api_key(args), "deepseek-cli")

        with patch.dict("os.environ", {"GLM_CODING_API_KEY": "glm-only"}, clear=True):
            args = parser.parse_args(["sample.pdf"])
            self.assertEqual(resolve_api_key(args), "glm-only")
            self.assertEqual(resolve_translation_api_key(args), "")

    def test_worker_count_precedence_and_legacy_compatibility(self) -> None:
        parser = build_parser()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_worker_counts(parser.parse_args(["sample.pdf"])), (4, 16))
            self.assertEqual(
                resolve_worker_counts(parser.parse_args(["sample.pdf", "--concurrency", "7"])),
                (7, 7),
            )
            self.assertEqual(
                resolve_worker_counts(
                    parser.parse_args(
                        ["sample.pdf", "--concurrency", "7", "--ocr-concurrency", "2"]
                    )
                ),
                (2, 7),
            )
            self.assertEqual(
                resolve_worker_counts(
                    parser.parse_args(
                        [
                            "sample.pdf",
                            "--concurrency",
                            "7",
                            "--translation-concurrency",
                            "9",
                        ]
                    )
                ),
                (7, 9),
            )

    def test_build_translation_client_routes_deepseek(self) -> None:
        parser = build_parser()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "deepseek-sentinel"}, clear=True):
            args = parser.parse_args(["sample.pdf"])
            client = build_translation_client(args, glm_api_base="https://glm.invalid")
        self.assertIsInstance(client, DeepSeekClient)
        assert isinstance(client, DeepSeekClient)
        self.assertEqual(client.api_base, "https://api.deepseek.com")
        self.assertEqual(client.text_model, "deepseek-v4-pro")
        self.assertNotIn("deepseek-sentinel", repr(client.__dict__).replace(client.api_key, ""))

    def test_api_timeout_cli(self) -> None:
        args = build_parser().parse_args(["sample.pdf", "--api-timeout", "37"])
        self.assertEqual(args.api_timeout, 37)

    def test_parse_page_spec(self) -> None:
        self.assertEqual(parse_page_spec("2-4, 7,4"), [2, 3, 4, 7])
        with self.assertRaises(ValueError):
            parse_page_spec("4-2")

    def test_clean_ocr_text(self) -> None:
        self.assertEqual(clean_ocr_text("```\n# 标题\n正文\n```"), "# 标题\n正文")
        self.assertEqual(
            clean_ocr_text("```markdown\n# 标题\n正文\n```\n\n**Quality Notes**\n清晰"),
            "# 标题\n正文",
        )
        self.assertEqual(clean_ocr_text("# 标题\n正文"), "# 标题\n正文")
        self.assertEqual(clean_ocr_text("plaintext\n日本語本文"), "日本語本文")
        self.assertEqual(
            clean_ocr_text(
                "Extracted Text\n\n正文\n\nContent Type: 书页文字\nLanguage/Format: Markdown"
            ),
            "正文",
        )
        self.assertEqual(
            clean_ocr_text("### Extracted Text\n\n正文\n\n### Content Type\n书页文字"),
            "正文",
        )
        self.assertEqual(
            clean_ocr_text(
                "Extracted Text\n\nContent Type: 书页文字\nQuality Notes: 截图内容为空白。"
            ),
            "",
        )
        self.assertEqual(clean_ocr_text("```\n```\n\n正文"), "正文")
        self.assertEqual(
            clean_ocr_text(
                "```\n```\n\n**Content Type** 空白书页\n\n**Quality Notes** 截图内容为空白。"
            ),
            "",
        )
        self.assertEqual(
            clean_ocr_text("正文\n\n```\n[无法辨认]\n```"),
            "正文",
        )
        self.assertEqual(
            clean_ocr_text(
                "**Extracted Text**\n\n正文\n\n**Content Type**  \n书籍\n\n**Quality Notes**  \n清晰"
            ),
            "正文",
        )

    def test_clean_translation_text_removes_model_notes(self) -> None:
        source = """修正说明：OCR 有破损，以下进行了整理。

第一段译文。

第二段译文。

***
*译者注：这不是原书内容。*
*[注1] 模型自行添加的解释。*
"""
        self.assertEqual(clean_translation_text(source), "第一段译文。\n\n第二段译文。")

    def test_clean_translation_text_removes_inline_transition_line(self) -> None:
        source = "第一段。\n\n在段能乐堂，翻译如下。\n\n第二段。"
        self.assertEqual(clean_translation_text(source), "第一段。\n\n第二段。")

    def test_publication_metadata_is_removed(self) -> None:
        source = """# 第一章

<!-- source-pdf: source.pdf -->
<!-- pdf-pages: 4-5 -->

<span epub:type="pagebreak" id="pdf-page-4" title="4"></span>
<!-- PDF_PAGE: 4 -->

第一页正文。

1

<span epub:type="pagebreak" id="pdf-page-5" title="5"></span>
<!-- PDF_PAGE: 5 -->

第二页正文。

2
"""
        result = strip_publication_metadata(source)
        self.assertEqual(result, "# 第一章\n\n第一页正文。\n\n第二页正文。\n")

    def test_reader_output_removes_source_page_but_keeps_sections_and_years(self) -> None:
        source = """# 章节

正文。

119

后文。

2

1921

年表结束。
"""
        self.assertEqual(
            strip_publication_metadata(source),
            "# 章节\n\n正文。\n\n119\n\n后文。\n\n2\n\n1921\n\n年表结束。\n",
        )

    def test_reader_removes_numeric_header_only_at_page_boundary(self) -> None:
        source = """# 章节

正文中的数据。

119

<span epub:type="pagebreak" id="pdf-page-120" title="120"></span>
<!-- PDF_PAGE: 120 -->

120

下一页正文。
"""
        self.assertEqual(
            strip_publication_metadata(source),
            "# 章节\n\n正文中的数据。\n\n下一页正文。\n",
        )

    def test_reader_has_only_one_h1_per_chapter(self) -> None:
        source = "# 章节\n\n正文。\n\n# OCR 子标题\n\n后文。\n"
        self.assertEqual(
            strip_publication_metadata(source),
            "# 章节\n\n正文。\n\n## OCR 子标题\n\n后文。\n",
        )

    def test_reader_demotes_setext_and_html_h1(self) -> None:
        source = "# 章节\n\n子标题\n====\n\n<h1>另一子标题</h1>\n"
        self.assertEqual(
            strip_publication_metadata(source),
            "# 章节\n\n子标题\n---\n\n<h2>另一子标题</h2>\n",
        )

    def test_reader_keeps_year_and_only_discards_one_leading_page_number(self) -> None:
        source = """# 年表

1930

<span epub:type="pagebreak" id="pdf-page-12" title="12"></span>
<!-- PDF_PAGE: 12 -->

— 12 —

42

后文。
"""
        self.assertEqual(
            strip_publication_metadata(source),
            "# 年表\n\n1930\n\n42\n\n后文。\n",
        )

    def test_publication_running_title_is_removed(self) -> None:
        source = """# 消费／消灭之理——《魔法使之夜》与〈普遍经济〉

正文。

87 消费／消灭之理——《魔法使之夜》与〈普通经济〉｜会良ひめる

后文。
"""
        result = strip_publication_metadata(
            source,
            publication_title="消费／消灭之理——《魔法使之夜》与〈普遍经济〉",
        )
        self.assertEqual(
            result,
            "# 消费／消灭之理——《魔法使之夜》与〈普遍经济〉\n\n正文。\n\n后文。\n",
        )
        noisy = """# 消费／消灭之理——《魔法使之夜》与〈普遍经济〉

正文。

消费/消灭的理——《魔法使之夜》与〈普通经济〉[原文存疑] 會良ひめろ[原文存疑]
"""
        self.assertEqual(
            strip_publication_metadata(noisy, publication_title="消费／消灭之理——《魔法使之夜》与〈普遍经济〉"),
            "# 消费／消灭之理——《魔法使之夜》与〈普遍经济〉\n\n正文。\n",
        )

    def test_chapter_running_title_and_page_number_are_removed(self) -> None:
        source = """# 第一章 歌德的《浮士德》：发展的悲剧

正文。

第一章 歌德的《浮士德》：发展的悲剧 47

后文。
"""
        self.assertEqual(
            strip_publication_metadata(
                source,
                publication_title="一切坚固的东西都烟消云散了",
                chapter_title="第一章 歌德的《浮士德》：发展的悲剧",
            ),
            "# 第一章 歌德的《浮士德》：发展的悲剧\n\n正文。\n\n后文。\n",
        )

    def test_markdown_formatted_short_running_title_is_removed(self) -> None:
        source = """# 讲故事的人 论尼古拉·列斯科夫

正文。

# 启迪

后文。
"""
        self.assertEqual(
            strip_publication_metadata(
                source,
                publication_title="启迪 本雅明文选",
                chapter_title="讲故事的人 论尼古拉·列斯科夫",
            ),
            "# 讲故事的人 论尼古拉·列斯科夫\n\n正文。\n\n后文。\n",
        )

    def test_short_running_title_is_removed_before_cross_page_join(self) -> None:
        source = """# 企鹅版前言：宽广开放的理解方式

《地下室手

<span epub:type="pagebreak" id="pdf-page-14" title="14"></span>
<!-- PDF_PAGE: 14 -->

企鹅版前言 5

记》中继续讨论。
"""
        self.assertEqual(
            strip_publication_metadata(
                source,
                publication_title="一切坚固的东西都烟消云散了：现代性体验",
                chapter_title="企鹅版前言：宽广开放的理解方式",
            ),
            "# 企鹅版前言：宽广开放的理解方式\n\n《地下室手记》中继续讨论。\n",
        )
        self.assertEqual(
            strip_publication_metadata(
                "# 现代性研究译丛·总序\n\n正文。\n\n总序 3\n\n后文。",
                publication_title="一切坚固的东西都烟消云散了：现代性体验",
                chapter_title="现代性研究译丛·总序",
            ),
            "# 现代性研究译丛·总序\n\n正文。\n\n后文。\n",
        )

    def test_publication_joins_only_cross_page_sentence_fragments(self) -> None:
        source = """# 章节

这一页结束于作为对

25

<span epub:type="pagebreak" id="pdf-page-26" title="26"></span>
<!-- PDF_PAGE: 26 -->

象展开的讨论。

完整句子。

26

<span epub:type="pagebreak" id="pdf-page-27" title="27"></span>
<!-- PDF_PAGE: 27 -->

下一段另起。
"""
        self.assertEqual(
            strip_publication_metadata(source),
            "# 章节\n\n这一页结束于作为对象展开的讨论。\n\n完整句子。\n\n下一段另起。\n",
        )

    def test_publication_does_not_join_signed_note_to_next_page(self) -> None:
        source = """# 第一章

① Wells，美国电影演员。——译者

<span epub:type="pagebreak" id="pdf-page-60" title="60"></span>
<!-- PDF_PAGE: 60 -->

处于我的绝对控制之下！
"""
        self.assertEqual(
            strip_publication_metadata(source),
            "# 第一章\n\n① Wells，美国电影演员。——译者\n\n处于我的绝对控制之下！\n",
        )

    def test_publication_does_not_join_chronology_year_at_page_boundary(self) -> None:
        source = """# 年表

前一年条目 GS VI 185—187

<span epub:type="pagebreak" id="pdf-page-290" title="290"></span>
<!-- PDF_PAGE: 290 -->

启迪

1930

文学批评纲要
"""
        self.assertEqual(
            strip_publication_metadata(source, publication_title="启迪 本雅明文选"),
            "# 年表\n\n前一年条目 GS VI 185—187\n\n1930\n\n文学批评纲要\n",
        )

    def test_docx_inline_markup_becomes_plain_text(self) -> None:
        self.assertEqual(
            markdown_inline_to_plain_text("术语<sup>[原文存疑]</sup>与[链接](https://example.test)"),
            "术语[原文存疑]与链接",
        )

    def test_remove_multiline_duplicate_title(self) -> None:
        text = "第一章\n\n陀思妥耶夫斯基的复调\n小说和评论著述对它的阐释\n\n正文"
        title = "第一章 陀思妥耶夫斯基的复调小说和评论著述对它的阐释"
        self.assertEqual(remove_duplicate_title(text, title), "正文")

    def test_remove_multiline_duplicate_title_with_margin_page_number(self) -> None:
        text = "37 第一章 歌德的《浮士德》：\n发展的悲剧\n\n正文"
        title = "第一章 歌德的《浮士德》：发展的悲剧"
        self.assertEqual(remove_duplicate_title(text, title), "正文")

    def test_remove_duplicate_title_ignores_running_header_markup_and_particles(self) -> None:
        text = "小林秀雄全集第三卷\n\n**小说の问题 I**\n\n正文"
        self.assertEqual(remove_duplicate_title(text, "小说的问题 I"), "正文")

    def test_parallel_translation_checkpoints(self) -> None:
        class FakeTranslator:
            def translate(self, text: str, *, source_language: str, target_language: str) -> str:
                return f"{target_language}:{text}"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            records = [
                PageRecord(1, "日本語の本文です。", language="ja"),
                PageRecord(2, "中文正文。", language="zh"),
                PageRecord(3, "別の日本語です。", language="ja"),
            ]
            translate_non_chinese_pages(
                records,
                output,
                FakeTranslator(),
                target_language="简体中文",
                force=False,
                concurrency=2,
                request_delay=0,
            )
            self.assertTrue((output / "pages/page_0001.json").exists())
            self.assertFalse((output / "pages/page_0002.json").exists())
            self.assertTrue(records[0].translated_text.startswith("简体中文:"))
            self.assertTrue(records[2].translated_text.startswith("简体中文:"))
            self.assertTrue(records[0].translation_is_fresh)
            self.assertTrue(records[2].translation_is_fresh)

    def test_translation_reruns_when_ocr_text_changed(self) -> None:
        class CountingTranslator:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, text: str, *, source_language: str, target_language: str) -> str:
                self.calls += 1
                return f"译文：{text}"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = PageRecord(1, "最初の本文", language="ja")
            translator = CountingTranslator()
            translate_non_chinese_pages(
                [record],
                output,
                translator,
                target_language="简体中文",
                force=False,
                concurrency=1,
            )
            self.assertEqual(translator.calls, 1)
            record.text = "修正後の本文"
            save_page_record(output, record)
            translate_non_chinese_pages(
                [record],
                output,
                translator,
                target_language="简体中文",
                force=False,
                concurrency=1,
            )
            self.assertEqual(translator.calls, 2)
            self.assertTrue(record.translation_is_fresh)

    def test_translation_reruns_when_model_changed(self) -> None:
        class CountingTranslator:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, text: str, *, source_language: str, target_language: str) -> str:
                self.calls += 1
                return f"译文：{text}"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = PageRecord(1, "日本語本文", language="ja")
            translator = CountingTranslator()
            translate_non_chinese_pages(
                [record],
                output,
                translator,
                target_language="简体中文",
                force=False,
                concurrency=1,
                translation_provider="deepseek",
                translation_model="deepseek-v4-flash",
            )
            translate_non_chinese_pages(
                [record],
                output,
                translator,
                target_language="简体中文",
                force=False,
                concurrency=1,
                translation_provider="deepseek",
                translation_model="deepseek-v4-pro",
            )
            self.assertEqual(translator.calls, 2)
            self.assertEqual(record.translation_provider, "deepseek")
            self.assertEqual(record.translation_model, "deepseek-v4-pro")
            self.assertEqual(record.translation_target_language, "简体中文")

    def test_import_does_not_mark_unverified_translation_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            (source / "pages").mkdir(parents=True)
            (source / "pages/page_0001.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 1,
                        "text": "更新后的OCR",
                        "language": "ja",
                        "translated_text": "旧译文",
                        "translation_source_sha256": "does-not-match",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(import_existing_ocr(source, output), 1)
            record = PageRecord(
                **json.loads((output / "pages/page_0001.json").read_text(encoding="utf-8"))
            )
            self.assertFalse(record.translation_is_fresh)
            self.assertEqual(record.compile_text, "更新后的OCR")

    def test_translation_workers_actually_overlap(self) -> None:
        barrier = threading.Barrier(2)

        class BarrierTranslator:
            def translate(self, text: str, *, source_language: str, target_language: str) -> str:
                barrier.wait(timeout=2)
                return f"{target_language}:{text}"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            records = [
                PageRecord(1, "日本語一。", language="ja"),
                PageRecord(2, "日本語二。", language="ja"),
            ]
            translate_non_chinese_pages(
                records,
                output,
                BarrierTranslator(),
                target_language="简体中文",
                force=False,
                concurrency=2,
            )
            self.assertTrue(all(record.translated_text for record in records))

    def test_detect_language(self) -> None:
        self.assertEqual(detect_language("这是一本中文书籍，包含足够多的中文文字用于判断。"), "zh")
        self.assertEqual(detect_language("これは日本語の文章です。日本語として判定されます。"), "ja")
        self.assertEqual(detect_language("This is a sufficiently long English sentence for language detection."), "en")

    def test_coding_plan_vision_mcp_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "fake_mcp.py"
            server.write_text(
                """
import json
import sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "extract_text_from_screenshot", "inputSchema": {"type": "object", "properties": {"image_path": {"type": "string"}}, "required": ["image_path"]}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "# 识别标题\\n\\n识别正文"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""".strip()
                + "\n",
                encoding="utf-8",
            )
            image = root / "page.jpg"
            image.write_bytes(b"fake")
            backend = CodingPlanVisionOCR(api_key="test-key", command=f"{sys.executable} -u {server}")
            try:
                text, request_id = backend.ocr_image(image)
            finally:
                backend.close()
            self.assertEqual(text, "# 识别标题\n\n识别正文")
            self.assertTrue(request_id.startswith("mcp-"))

    def test_coding_plan_content_filter_uses_segmented_fallback(self) -> None:
        class FilterThenReadClient:
            def extract_text(self, image_path: Path) -> tuple[str, str]:
                if "_segment_" not in image_path.stem:
                    raise RuntimeError(
                        'HTTP 400: {"contentFilter":[],"error":{"code":"1301",'
                        '"message":"potentially unsafe content"}}'
                    )
                match = re.search(r"_segment_(\d+)_", image_path.stem)
                assert match is not None
                values = {"1": "（无可见文字）", "2": "彼得", "3": "堡正文。", "4": "结尾。"}
                return values[match.group(1)], "mcp-test"

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.jpg"
            Image.new("RGB", (240, 800), "white").save(image_path)
            backend = object.__new__(CodingPlanVisionOCR)
            with patch.object(backend, "_client", return_value=FilterThenReadClient()):
                text, request_id = backend.ocr_image(image_path)
            self.assertEqual(text, "彼得堡正文。\n\n结尾。")
            self.assertTrue(request_id.startswith("mcp-segmented-"))
            self.assertEqual(list(image_path.parent.glob("*_segment_*.jpg")), [])

    def test_vertical_segment_fallback_reads_right_to_left(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "vertical.jpg"
            image = Image.new("L", (240, 400), 0)
            image.paste(255, (116, 0, 240, 400))
            image.save(image_path)
            backend = object.__new__(CodingPlanVisionOCR)
            backend.reading_direction = "vertical"

            def read_band(path: Path, *, depth: int) -> tuple[str, str]:
                with Image.open(path).convert("L") as band:
                    average = sum(band.get_flattened_data()) / (band.width * band.height)
                    return ("右。" if average > 127 else "左。"), "test"

            with patch.object(backend, "_ocr_filtered_band", side_effect=read_band):
                text, request_id = backend._ocr_segmented(image_path, object(), segments=2)
            self.assertEqual(text, "右。\n\n左。")
            self.assertTrue(request_id.startswith("mcp-segmented-"))
            self.assertEqual(list(image_path.parent.glob("*_segment_*.jpg")), [])


class MappingAndCompilationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pdf_path = self.root / "sample.pdf"
        document = fitz.open()
        for page_number in range(1, 13):
            page = document.new_page()
            page.insert_text((72, 72), f"PDF page {page_number}")
        document.save(self.pdf_path)
        document.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def sample_entries() -> list[TocEntry]:
        return [
            TocEntry("toc-1", "第一章", "起点", 1, "chapter", 1),
            TocEntry("toc-2", "第一节", "细节", 2, "section", 3),
            TocEntry("toc-2b", "第二节", "深入", 2, "section", 5),
            TocEntry("toc-3", "第二章", "终点", 1, "chapter", 6),
        ]

    @staticmethod
    def sample_records() -> list[PageRecord]:
        records = [PageRecord(page, f"这是 PDF 第 {page} 页的正文内容。") for page in range(1, 13)]
        records[3] = PageRecord(4, "# 第一章 起点\n第一章正文")
        records[5] = PageRecord(6, "## 第一节 细节\n分节正文")
        records[7] = PageRecord(8, "## 第二节 深入\n分节正文")
        records[8] = PageRecord(9, "# 第二章 终点\n第二章正文")
        return records

    def test_infer_page_offset(self) -> None:
        offset, evidence = infer_page_offset(self.sample_entries(), self.sample_records(), toc_end=2)
        self.assertEqual(offset, 3)
        self.assertGreaterEqual(len(evidence), 3)

    def test_manual_offset_is_authoritative(self) -> None:
        payload = {
            "toc_pdf_pages": [2],
            "entries": [entry.__dict__ for entry in self.sample_entries()],
        }
        records = self.sample_records()
        records[3] = PageRecord(4, "无关内容")
        records[4] = PageRecord(5, "# 第一章 起点\n错误的标题匹配位置")
        mapped = apply_page_mapping(payload, records, page_offset=3)
        first = mapped["entries"][0]
        self.assertEqual(first["pdf_page"], 4)

    def test_title_evidence_handles_piecewise_page_offsets(self) -> None:
        entries = [
            TocEntry("preface", "", "前言", 1, "frontmatter", 1),
            TocEntry("chapter", "第一章", "起点", 1, "chapter", 6),
            TocEntry("ending", "", "结语", 1, "other", 10),
        ]
        records = [PageRecord(page, f"普通正文 {page}") for page in range(1, 13)]
        records[3] = PageRecord(4, "前言\n正文")
        records[7] = PageRecord(8, "第一章 起点\n正文")
        records[11] = PageRecord(12, "结语\n正文")
        payload = {"toc_pdf_pages": [2], "entries": [entry.__dict__ for entry in entries]}
        mapped = apply_page_mapping(payload, records, page_offset=None)
        self.assertEqual(mapped["page_offset"], 2)
        self.assertEqual([item["pdf_page"] for item in mapped["entries"]], [4, 8, 12])

    def test_ocr_checkpoints_and_page_markdown(self) -> None:
        class FakeOCR:
            ocr_model = "fake-ocr"

            def ocr_image(self, image_path: Path) -> tuple[str, str]:
                self.assert_image(image_path)
                return "# 页标题\n\n逐页正文", "request-test"

            @staticmethod
            def assert_image(image_path: Path) -> None:
                if not image_path.exists() or image_path.stat().st_size == 0:
                    raise AssertionError("Rendered page image is missing")

            def close(self) -> None:
                return

        output = self.root / "ocr-output"
        records = ocr_pdf(
            self.pdf_path,
            output,
            FakeOCR(),
            start_page=1,
            end_page=2,
            concurrency=2,
            dpi=96,
            max_image_side=1000,
            jpeg_quality=80,
            keep_page_images=False,
            force=False,
        )
        self.assertEqual([record.pdf_page for record in records], [1, 2])
        self.assertTrue((output / "pages" / "page_0001.json").exists())
        self.assertEqual((output / "pages" / "page_0001.md").read_text(encoding="utf-8"), "# 页标题\n\n逐页正文\n")

    def test_ocr_cache_model_prefix_replaces_fallback(self) -> None:
        class FakeOCR:
            def __init__(self, model: str) -> None:
                self.ocr_model = model

            def ocr_image(self, image_path: Path) -> tuple[str, str]:
                return f"由 {self.ocr_model} 识别", "request"

            def close(self) -> None:
                return

        output = self.root / "model-prefix-output"
        ocr_pdf(
            self.pdf_path,
            output,
            FakeOCR("tesseract/test"),
            start_page=1,
            end_page=1,
            concurrency=1,
            dpi=72,
            max_image_side=800,
            jpeg_quality=80,
            keep_page_images=False,
            force=False,
        )
        records = ocr_pdf(
            self.pdf_path,
            output,
            FakeOCR("coding-plan/test"),
            start_page=1,
            end_page=1,
            concurrency=1,
            dpi=72,
            max_image_side=800,
            jpeg_quality=80,
            keep_page_images=False,
            force=False,
            cache_model_prefix="coding-plan/",
        )
        self.assertEqual(records[0].ocr_model, "coding-plan/test")

    def test_chapter_and_section_boundaries(self) -> None:
        payload = {
            "toc_pdf_pages": [2],
            "entries": [entry.__dict__ for entry in self.sample_entries()],
        }
        mapped = apply_page_mapping(payload, self.sample_records(), page_offset=3)

        chapter_output = self.root / "chapter-output"
        chapter_manifest, rows = compile_chapters(
            self.pdf_path,
            chapter_output,
            self.sample_records(),
            mapped,
            granularity="chapter",
        )
        self.assertEqual([(item["pdf_page"], item["end_pdf_page"]) for item in chapter_manifest], [(4, 8), (9, 12)])
        self.assertTrue(rows)
        first_markdown = (chapter_output / "chapters" / chapter_manifest[0]["filename"]).read_text(encoding="utf-8")
        self.assertTrue(first_markdown.startswith("# 第一章 起点\n"))
        self.assertEqual(first_markdown.count("# 第一章 起点"), 1)

        section_output = self.root / "section-output"
        section_manifest, _ = compile_chapters(
            self.pdf_path,
            section_output,
            self.sample_records(),
            mapped,
            granularity="section",
        )
        self.assertEqual(
            [(item["pdf_page"], item["end_pdf_page"]) for item in section_manifest],
            [(6, 8), (8, 8)],
        )
        self.assertEqual(section_manifest[0]["boundary_mode"], "closed-overlap")

    def test_chapter_compilation_keeps_top_level_book_matter(self) -> None:
        entries = [
            TocEntry("preface", "", "中译本前言", 1, "frontmatter", 1, pdf_page=1),
            TocEntry("chapter", "第一章", "正文", 1, "chapter", 4, pdf_page=4),
            TocEntry("section", "第一节", "细节", 2, "section", 6, pdf_page=6),
            TocEntry("ending", "", "结语", 1, "other", 10, pdf_page=10),
        ]
        payload = {"page_offset": 0, "entries": [entry.__dict__ for entry in entries]}
        manifest, _ = compile_chapters(
            self.pdf_path,
            self.root / "complete-book",
            self.sample_records(),
            payload,
            granularity="chapter",
        )
        self.assertEqual(
            [item["display_title"] for item in manifest],
            ["中译本前言", "第一章 正文", "结语"],
        )
        self.assertEqual(
            [(item["pdf_page"], item["end_pdf_page"]) for item in manifest],
            [(1, 3), (4, 9), (10, 12)],
        )

    def test_compile_translation_gate_and_null_frontmatter_printed_page(self) -> None:
        record = PageRecord(
            1,
            "日本語本文",
            language="ja",
            translated_text="中文译文",
        )
        payload = {
            "page_offset": 10,
            "entries": [
                TocEntry(
                    "preface",
                    "",
                    "前言",
                    1,
                    "frontmatter",
                    None,
                    pdf_page=1,
                ).__dict__
            ],
        }
        with self.assertRaisesRegex(ValueError, "Missing or stale translation"):
            compile_chapters(
                self.pdf_path,
                self.root / "stale-translation",
                [record],
                payload,
                granularity="chapter",
                require_translation=True,
            )
        record.translation_source_sha256 = record.text_sha256
        record.translation_provider = "deepseek"
        record.translation_model = "deepseek-v4-pro"
        record.translation_target_language = "简体中文"
        _, rows = compile_chapters(
            self.pdf_path,
            self.root / "fresh-translation",
            [record],
            payload,
            granularity="chapter",
            require_translation=True,
        )
        self.assertEqual(rows[0]["printed_page"], None)

    def test_compile_preflight_preserves_previous_chapters_on_failure(self) -> None:
        output = self.root / "preflight-preserves-output"
        chapter_dir = output / "chapters"
        chapter_dir.mkdir(parents=True)
        old = chapter_dir / "001_previous.md"
        old.write_text("# 上次成功产物\n", encoding="utf-8")
        payload = {
            "page_offset": 0,
            "entries": [
                TocEntry(
                    "chapter",
                    "第一章",
                    "正文",
                    1,
                    "chapter",
                    1,
                    pdf_page=1,
                ).__dict__
            ],
        }
        with self.assertRaisesRegex(ValueError, "Missing or stale translation"):
            compile_chapters(
                self.pdf_path,
                output,
                [PageRecord(1, "日本語", language="ja")],
                payload,
                granularity="chapter",
                require_translation=True,
            )
        self.assertEqual(old.read_text(encoding="utf-8"), "# 上次成功产物\n")

    def test_epub_and_bookmarked_pdf(self) -> None:
        output = self.root / "book"
        chapter_dir = output / "chapters"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "001_第一章.md").write_text(
            """# 第一章

<!-- source-pdf: sample.pdf -->
<!-- pdf-pages: 4-5 -->

<span epub:type="pagebreak" id="pdf-page-4" title="4"></span>
<!-- PDF_PAGE: 4 -->

正文第一页。

1

<span epub:type="pagebreak" id="pdf-page-5" title="5"></span>
<!-- PDF_PAGE: 5 -->

正文第二页。

2
""",
            encoding="utf-8",
        )
        manifest = [
            {
                "sequence": 1,
                "display_title": "第一章",
                "filename": "001_第一章.md",
            }
        ]
        epub_path = output / "sample.epub"
        build_epub(epub_path, chapter_dir, manifest, book_title="测试书", language="zh-CN")
        with zipfile.ZipFile(epub_path) as archive:
            self.assertEqual(archive.namelist()[0], "mimetype")
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            self.assertIn("OEBPS/nav.xhtml", archive.namelist())
            ET.fromstring(archive.read("OEBPS/nav.xhtml"))
            chapter_xml = archive.read("OEBPS/001_第一章.xhtml")
            ET.fromstring(chapter_xml)
            self.assertNotIn(b"source-pdf", chapter_xml)
            self.assertNotIn(b"PDF_PAGE", chapter_xml)
            self.assertNotIn(b"pdf-page-", chapter_xml)
            self.assertNotIn(b">1<", chapter_xml)

        docx_path = output / "sample.docx"
        build_docx(docx_path, chapter_dir, manifest, book_title="测试书")
        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read("word/document.xml")
            ET.fromstring(document_xml)
            self.assertNotIn(b"source-pdf", document_xml)
            self.assertNotIn(b"PDF_PAGE", document_xml)
            self.assertNotIn(b"pdf-page-", document_xml)
            self.assertNotIn(b'w:type="page"', document_xml)

        toc_payload = {
            "entries": [
                {
                    "id": "toc-1",
                    "index": "第一章",
                    "title": "起点",
                    "level": 1,
                    "kind": "chapter",
                    "printed_page": 1,
                    "pdf_page": 4,
                    "end_pdf_page": 8,
                },
                {
                    "id": "toc-end",
                    "index": "",
                    "title": "封底",
                    "level": 1,
                    "kind": "part",
                    "printed_page": None,
                    "pdf_page": 8,
                    "end_pdf_page": 8,
                }
            ]
        }
        bookmarked = output / "bookmarked.pdf"
        build_bookmarked_pdf(self.pdf_path, bookmarked, toc_payload)
        with fitz.open(bookmarked) as document:
            self.assertEqual(document.get_toc()[0][1:], ["第一章 起点", 4])
            self.assertEqual(len(document.get_toc()), 1)

    def test_cli_manual_toc_to_all_outputs(self) -> None:
        output = self.root / "cli-output"
        for record in self.sample_records():
            save_page_record(output, record)
        manual_toc = self.root / "manual-toc.json"
        manual_toc.write_text(
            json.dumps(
                {
                    "toc_pdf_pages": [2],
                    "entries": [entry.__dict__ for entry in self.sample_entries()],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            main(
                [
                    str(self.pdf_path),
                    "-o",
                    str(output),
                    "--phase",
                    "toc",
                    "--toc-json",
                    str(manual_toc),
                    "--page-offset",
                    "3",
                ]
            ),
            0,
        )
        self.assertEqual(
            main([str(self.pdf_path), "-o", str(output), "--phase", "compile", "--granularity", "chapter"]),
            0,
        )
        self.assertTrue((output / "knowledge_base.jsonl").exists())
        self.assertTrue((output / "sample.epub").exists())
        self.assertTrue((output / "sample.docx").exists())
        self.assertTrue((output / "sample_带目录.pdf").exists())


if __name__ == "__main__":
    unittest.main()
