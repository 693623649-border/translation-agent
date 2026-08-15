from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import fitz

from book_pipeline import (
    PageRecord,
    TocEntry,
    build_parser,
    load_page_records,
    main,
    ocr_pdf,
    resolve_expected_ocr_model_exact,
    save_page_record,
    write_json,
)
from frontend_app import OCR_ADAPTERS
from ocr_backends.paddle_local import (
    PaddleLocalOCR,
    PaddleOCRText,
    paddle_checkpoint_identity,
)
from pipeline_graph.book import (
    NODE_OCR,
    NODE_SOURCE,
    _ocr_stage_semantics,
    prepare_book_graph,
)
from pipeline_profiles import load_pipeline_profiles


PROFILE_TEMPLATE = """
[profiles.paddle]
adapter = "paddleocr-local"
provider = "local"
base_url = "unix://{socket_path}"
model = "PP-OCRv6_medium"
credential_env = ""
timeout = 120
concurrency = 16
reading_direction = "horizontal"

[profiles.paddle.content]
engine = "paddle_static"
text_detection_model = "PP-OCRv6_medium_det"
text_recognition_model = "PP-OCRv6_medium_rec"
text_det_limit_side_len = {side_len}
reading_order_version = "book-order-v1"

[profiles.paddle.runtime]
devices = ["gpu:0", "gpu:1"]
instances_per_device = {instances}
spool_dir = "{spool_dir}"
auto_start = false

[pipeline]
ocr_profile = "paddle"
"""


def write_profile(root: Path, *, side_len: int = 1280, instances: int = 2) -> Path:
    path = root / f"pipeline-{side_len}-{instances}.toml"
    path.write_text(
        PROFILE_TEMPLATE.format(
            socket_path=root / "paddle.sock",
            spool_dir=root / "spool",
            side_len=side_len,
            instances=instances,
        ),
        encoding="utf-8",
    )
    return path


def make_pdf(path: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=200, height=200)
        page.insert_text((20, 30), "test")
        document.save(path)


class PaddleIntegrationTests(unittest.TestCase):
    def test_parser_frontend_and_exact_identity_support_local_profile(self) -> None:
        self.assertIn("paddleocr-local", OCR_ADAPTERS)
        args = build_parser().parse_args(
            ["book.pdf", "--ocr-backend", "paddleocr-local"]
        )
        self.assertEqual(args.ocr_backend, "paddleocr-local")
        with tempfile.TemporaryDirectory() as directory:
            config = write_profile(Path(directory))
            profiles = load_pipeline_profiles(config)
            profile = profiles.for_stage("ocr")
            assert profile is not None
            args = build_parser().parse_args(
                ["book.pdf", "--config", str(config), "--ocr-profile", "paddle"]
            )
            self.assertEqual(
                resolve_expected_ocr_model_exact(args, profile),
                paddle_checkpoint_identity(
                    profile,
                    reading_direction="horizontal",
                    horizontal_columns=1,
                    dpi=args.dpi,
                    max_image_side=args.max_image_side,
                    jpeg_quality=args.jpeg_quality,
                ),
            )

    def test_render_settings_change_checkpoint_but_not_service_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = write_profile(root)
            profile = load_pipeline_profiles(config).for_stage("ocr")
            assert profile is not None
            base_args = build_parser().parse_args(
                ["book.pdf", "--config", str(config), "--ocr-profile", "paddle"]
            )
            high_dpi_args = build_parser().parse_args(
                [
                    "book.pdf",
                    "--config",
                    str(config),
                    "--ocr-profile",
                    "paddle",
                    "--dpi",
                    "300",
                ]
            )
            self.assertNotEqual(
                resolve_expected_ocr_model_exact(base_args, profile),
                resolve_expected_ocr_model_exact(high_dpi_args, profile),
            )
            base_client = PaddleLocalOCR.from_profile(
                profile,
                reading_direction="horizontal",
                dpi=base_args.dpi,
                max_image_side=base_args.max_image_side,
                jpeg_quality=base_args.jpeg_quality,
            )
            high_dpi_client = PaddleLocalOCR.from_profile(
                profile,
                reading_direction="horizontal",
                dpi=high_dpi_args.dpi,
                max_image_side=high_dpi_args.max_image_side,
                jpeg_quality=high_dpi_args.jpeg_quality,
            )
            self.assertEqual(
                base_client.service_identity,
                high_dpi_client.service_identity,
            )
            self.assertNotEqual(base_client.ocr_model, high_dpi_client.ocr_model)

    def test_graph_content_change_is_semantic_but_runtime_tuning_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = write_profile(root, side_len=1280, instances=2)
            runtime = write_profile(root, side_len=1280, instances=4)
            content = write_profile(root, side_len=960, instances=2)
            payloads = []
            for config in (base, runtime, content):
                args = build_parser().parse_args(
                    ["book.pdf", "--config", str(config), "--ocr-profile", "paddle"]
                )
                payloads.append(_ocr_stage_semantics(args))
        self.assertEqual(payloads[0], payloads[1])
        self.assertNotEqual(payloads[0], payloads[2])

    def test_graph_injects_exact_local_checkpoint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            make_pdf(source)
            output = root / "output"
            config = write_profile(root)
            profile = load_pipeline_profiles(config).for_stage("ocr")
            assert profile is not None
            expected = paddle_checkpoint_identity(
                profile,
                reading_direction="horizontal",
                horizontal_columns=1,
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            prepared = prepare_book_graph(
                [
                    str(source),
                    "-o",
                    str(output),
                    "--phase",
                    "ocr",
                    "--config",
                    str(config),
                    "--ocr-profile",
                    "paddle",
                ]
            )

            def fake_ocr(argv: list[str]) -> int:
                index = argv.index("--ocr-cache-model")
                self.assertEqual(argv[index + 1], expected)
                pages = output / "pages"
                pages.mkdir(parents=True)
                (pages / "page_0001.json").write_text(
                    json.dumps(
                        {"pdf_page": 1, "text": "正文", "ocr_model": expected}
                    ),
                    encoding="utf-8",
                )
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_ocr,
            ):
                result = prepared.execute()
        self.assertEqual(result.executed, (NODE_SOURCE, NODE_OCR))

    def test_compile_gate_rejects_complete_but_wrong_model_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            make_pdf(source)
            output = root / "output"
            config = write_profile(root)
            save_page_record(
                output,
                PageRecord(
                    1,
                    "第一章\n正文",
                    language="zh",
                    ocr_model="tesseract/chi_sim/psm-3",
                ),
            )
            entry = TocEntry(
                "chapter-1",
                "第一章",
                "",
                1,
                "chapter",
                1,
                pdf_page=1,
            )
            write_json(
                output / "toc.json",
                {
                    "page_offset": 0,
                    "printed_pages_per_pdf_page": 1,
                    "entries": [entry.__dict__],
                },
            )
            error_output = io.StringIO()
            with redirect_stderr(error_output):
                exit_code = main(
                    [
                        str(source),
                        "-o",
                        str(output),
                        "--phase",
                        "compile",
                        "--config",
                        str(config),
                        "--ocr-profile",
                        "paddle",
                        "--require-complete-ocr",
                        "--no-epub",
                        "--no-docx",
                        "--no-kb",
                        "--no-bookmarked-pdf",
                        "--no-verify",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("Exact OCR model", error_output.getvalue())
            self.assertFalse((output / "chapters.json").exists())

    def test_local_metadata_is_persisted_and_spool_is_cleaned(self) -> None:
        class FakeLocalBackend:
            ocr_model = "paddleocr-local/test/content-v1"
            jpeg_optimize = False

            def __init__(self, spool_dir: Path) -> None:
                self.spool_dir = spool_dir

            def ocr_image(self, image_path: Path):
                self.assert_image(image_path)
                return (
                    PaddleOCRText(
                        "本地识别正文",
                        {
                            "line_count": 2,
                            "mean_score": 0.98,
                            "minimum_score": 0.91,
                            "reading_direction": "horizontal",
                            "horizontal_columns": 1,
                            "layout_line_count": 1,
                            "layout_lines": [
                                {
                                    "text": "本地识别正文",
                                    "score": 0.98,
                                    "bbox": [10, 20, 110, 40],
                                    "polygon": [
                                        [10, 20],
                                        [110, 20],
                                        [110, 40],
                                        [10, 40],
                                    ],
                                }
                            ],
                            "api_key": "must-not-be-persisted",
                        },
                    ),
                    "local-request",
                )

            @staticmethod
            def assert_image(image_path: Path) -> None:
                if not image_path.is_file():
                    raise AssertionError(image_path)

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            make_pdf(source)
            output = root / "output"
            spool = root / "spool"
            backend = FakeLocalBackend(spool)
            ocr_pdf(
                source,
                output,
                backend,
                start_page=1,
                end_page=1,
                concurrency=1,
                dpi=100,
                max_image_side=1000,
                jpeg_quality=85,
                keep_page_images=False,
                force=False,
                cache_model_exact=backend.ocr_model,
            )
            record = load_page_records(output)[0]
            self.assertIn("ocr_line_count=2", record.notes)
            self.assertIn("ocr_mean_score=0.980000", record.notes)
            self.assertEqual(record.ocr_metadata["layout_line_count"], 1)
            self.assertEqual(record.ocr_metadata["schema_version"], 1)
            self.assertEqual(
                record.ocr_metadata["layout_lines"][0]["bbox"],
                [10.0, 20.0, 110.0, 40.0],
            )
            checkpoint = (output / "pages" / "page_0001.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("must-not-be-persisted", checkpoint)
            self.assertNotIn("api_key", checkpoint)
            self.assertFalse(any(spool.iterdir()))

    def test_temporary_rendered_page_and_directory_are_private(self) -> None:
        class PermissionRecordingBackend:
            ocr_model = "paddleocr-local/test/permissions"
            jpeg_optimize = False

            def __init__(self, spool_dir: Path) -> None:
                self.spool_dir = spool_dir
                self.observed_modes: tuple[int, int] | None = None

            def ocr_image(self, image_path: Path):
                self.observed_modes = (
                    image_path.parent.stat().st_mode & 0o777,
                    image_path.stat().st_mode & 0o777,
                )
                return "本地识别正文", "permission-request"

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            make_pdf(source)
            backend = PermissionRecordingBackend(root / "spool")
            ocr_pdf(
                source,
                root / "output",
                backend,
                start_page=1,
                end_page=1,
                concurrency=1,
                dpi=100,
                max_image_side=1000,
                jpeg_quality=85,
                keep_page_images=False,
                force=False,
                cache_model_exact=backend.ocr_model,
            )
            self.assertEqual(
                backend.observed_modes,
                (0o700, 0o600),
                "temporary OCR image directories/files must be private",
            )


if __name__ == "__main__":
    unittest.main()
