from __future__ import annotations

import errno
import io
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_ocr.paddle_service import (
    _RequestHandler,
    _acquire_runtime_lock,
    _extract_page,
    _pipeline_kwargs,
    _release_runtime_lock,
)
from local_ocr.protocol import receive_message, request as protocol_request, send_message
from local_ocr.reading_order import TextLine, order_text_lines
from local_ocr.runtime_paths import ensure_private_directory
from ocr_backends.paddle_local import (
    PaddleLocalOCR,
    paddle_checkpoint_identity,
    paddle_profile_identity,
    paddle_service_identity,
)
from ocr_backends.registry import registered_backends
from pipeline_profiles import ModelProfile


def line(text: str, left: float, top: float, right: float, bottom: float) -> TextLine:
    return TextLine(
        text=text,
        score=0.99,
        polygon=((left, top), (right, top), (right, bottom), (left, bottom)),
    )


class ReadingOrderTests(unittest.TestCase):
    def test_horizontal_rows_are_top_to_bottom_then_left_to_right(self) -> None:
        values = [
            line("右上", 60, 1, 90, 10),
            line("第二行", 1, 20, 80, 29),
            line("左上", 1, 0, 40, 9),
        ]
        ordered = order_text_lines(values, reading_direction="horizontal")
        self.assertEqual([value.text for value in ordered], ["左上", "右上", "第二行"])

    def test_vertical_japanese_is_right_to_left_then_top_to_bottom(self) -> None:
        values = [
            line("左列上", 10, 0, 20, 20),
            line("右列下", 70, 30, 80, 50),
            line("右列上", 70, 0, 80, 20),
        ]
        ordered = order_text_lines(values, reading_direction="vertical")
        self.assertEqual([value.text for value in ordered], ["右列上", "右列下", "左列上"])

    def test_vertical_column_clustering_tolerates_x_jitter(self) -> None:
        values = [
            line("右列下", 69, 30, 79, 50),
            line("右列上", 70, 0, 80, 20),
            line("左列上", 10, 0, 20, 20),
        ]
        ordered = order_text_lines(values, reading_direction="vertical")
        self.assertEqual([value.text for value in ordered], ["右列上", "右列下", "左列上"])

    def test_explicit_two_columns_are_not_interleaved(self) -> None:
        values = [
            line("左一", 0, 0, 35, 8),
            line("右一", 60, 0, 95, 8),
            line("左二", 0, 12, 35, 20),
            line("右二", 60, 12, 95, 20),
        ]
        ordered = order_text_lines(
            values,
            reading_direction="horizontal",
            horizontal_columns=2,
        )
        self.assertEqual([value.text for value in ordered], ["左一", "左二", "右一", "右二"])

    def test_horizontal_edge_vertical_running_heads_are_reassembled(self) -> None:
        values = [
            line("正文第一行足够长", 118, 0, 800, 12),
            line("正文第二行足够长", 118, 30, 810, 42),
            line("正文第三行足够长", 118, 60, 790, 72),
            line("中", 74, 100, 94, 120),
            line("产", 75, 124, 95, 144),
            line("阶", 73, 148, 93, 168),
            line("级", 74, 172, 94, 192),
            line("的", 74, 196, 94, 216),
            # Paddle may return more than one glyph in a tall vertical box.
            line("然子", 74, 220, 94, 280),
            line("们", 75, 284, 95, 304),
            line("第一章", 930, 100, 950, 160),
            line("历史", 929.5, 164, 949.5, 204),
            line("想象力", 930.5, 208, 950.5, 268),
        ]
        ordered = order_text_lines(values, reading_direction="horizontal")
        self.assertEqual(
            [value.text for value in ordered],
            [
                "正文第一行足够长",
                "正文第二行足够长",
                "正文第三行足够长",
                "第一章历史想象力",
                "中产阶级的然子们",
            ],
        )

    def test_body_lists_and_code_glyphs_are_not_reassembled(self) -> None:
        values = [
            line("正文第一行足够长", 80, 0, 900, 12),
            line("正文第二行足够长", 80, 25, 900, 37),
            line("正文第三行足够长", 80, 50, 900, 62),
            # Narrow list labels and code identifiers are inside the body.
            line("甲", 100, 90, 110, 100),
            line("乙", 100, 110, 110, 120),
            line("丙", 100, 130, 110, 140),
            line("类", 160, 170, 170, 180),
            line("名", 160, 190, 170, 200),
            line("值", 160, 210, 170, 220),
        ]
        ordered = order_text_lines(values, reading_direction="horizontal")
        texts = [value.text for value in ordered]
        self.assertEqual(len(texts), len(values))
        self.assertNotIn("甲乙丙", texts)
        self.assertNotIn("类名值", texts)

    def test_edge_false_positives_remain_separate(self) -> None:
        values = [
            line("正文第一行足够长", 100, 0, 800, 12),
            line("正文第二行足够长", 100, 25, 800, 37),
            line("正文第三行足够长", 100, 50, 800, 62),
            # Two CJK glyphs are below the minimum evidence threshold.
            line("甲", 20, 90, 30, 100),
            line("乙", 20, 110, 30, 120),
            # One detection block is not fragmentation, even if it is tall.
            line("中产阶级", 920, 90, 930, 160),
            # Horizontal CJK fragments at the edge are not a vertical column.
            line("章节", 20, 260, 50, 270),
            line("标题", 20, 275, 50, 285),
            # ASCII source-code fragments never qualify as a running head.
            line("if", 40, 180, 50, 190),
            line("x", 40, 200, 50, 210),
            line("else", 40, 220, 50, 230),
        ]
        ordered = order_text_lines(values, reading_direction="horizontal")
        texts = [value.text for value in ordered]
        self.assertEqual(len(texts), len(values))
        self.assertNotIn("甲乙", texts)
        self.assertIn("中产阶级", texts)
        self.assertNotIn("ifxelse", texts)


class PaddleBackendTests(unittest.TestCase):
    def profile(self, root: Path) -> ModelProfile:
        return ModelProfile(
            name="paddle",
            adapter="paddleocr-local",
            provider="local",
            base_url=f"unix://{root / 'paddle.sock'}",
            model="PP-OCRv6_medium",
            concurrency=16,
            reading_direction="horizontal",
            content={
                "engine": "paddle_static",
                "text_detection_model": "PP-OCRv6_medium_det",
                "text_recognition_model": "PP-OCRv6_medium_rec",
                "reading_order_version": "book-order-v1",
            },
            runtime={
                "devices": ["gpu:0", "gpu:1"],
                "instances_per_device": 2,
                "spool_dir": str(root / "spool"),
                "auto_start": False,
            },
        )

    def test_runtime_tuning_does_not_change_checkpoint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self.profile(root)
            changed_runtime = replace(
                profile,
                runtime={**dict(profile.runtime), "instances_per_device": 4},
            )
            changed_content = replace(
                profile,
                content={**dict(profile.content), "text_det_limit_side_len": 960},
            )
            self.assertEqual(
                paddle_profile_identity(profile),
                paddle_profile_identity(changed_runtime),
            )
            self.assertNotEqual(
                paddle_profile_identity(profile),
                paddle_profile_identity(changed_content),
            )
            self.assertNotEqual(
                paddle_profile_identity(profile, horizontal_columns=1),
                paddle_profile_identity(profile, horizontal_columns=2),
            )
            self.assertEqual(
                paddle_service_identity(profile),
                paddle_service_identity(changed_runtime),
            )
            self.assertEqual(
                paddle_checkpoint_identity(
                    profile,
                    dpi=200,
                    max_image_side=3000,
                    jpeg_quality=90,
                ),
                paddle_checkpoint_identity(
                    changed_runtime,
                    dpi=200,
                    max_image_side=3000,
                    jpeg_quality=90,
                ),
            )
            baseline_checkpoint = paddle_checkpoint_identity(
                profile,
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            for render_change in (
                {"dpi": 300, "max_image_side": 3000, "jpeg_quality": 90},
                {"dpi": 200, "max_image_side": 4000, "jpeg_quality": 90},
                {"dpi": 200, "max_image_side": 3000, "jpeg_quality": 95},
            ):
                self.assertNotEqual(
                    baseline_checkpoint,
                    paddle_checkpoint_identity(profile, **render_change),
                )

    def test_client_validates_service_identity_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "page.jpg"
            image.write_bytes(b"fake")
            backend = PaddleLocalOCR.from_profile(
                self.profile(root),
                reading_direction="horizontal",
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            responses = [
                {
                    "ok": True,
                    "ready": True,
                    "model_identity": backend.service_identity,
                },
                {
                    "ok": True,
                    "text": "识别正文",
                    "metadata": {"line_count": 1, "mean_score": 0.98},
                    "model_identity": backend.service_identity,
                },
            ]
            with patch(
                "ocr_backends.paddle_local.request",
                side_effect=responses,
            ):
                text, request_id = backend.ocr_image(image)
            self.assertEqual(text, "识别正文")
            self.assertTrue(request_id.startswith("paddle-"))
            self.assertEqual(text.ocr_metadata["line_count"], 1)

    def test_client_preserves_service_error_without_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "page.jpg"
            image.write_bytes(b"fake")
            backend = PaddleLocalOCR.from_profile(
                self.profile(root),
                reading_direction="horizontal",
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            responses = [
                {
                    "ok": True,
                    "ready": True,
                    "model_identity": backend.service_identity,
                },
                {"ok": False, "error": "TimeoutError: request timed out"},
            ]
            with patch(
                "ocr_backends.paddle_local.request",
                side_effect=responses,
            ):
                with self.assertRaisesRegex(RuntimeError, "request timed out"):
                    backend.ocr_image(image)

    def test_service_configs_are_unique_even_for_same_content_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_profile = self.profile(root)
            second_profile = replace(
                first_profile,
                runtime={
                    **dict(first_profile.runtime),
                    "socket_path": str(root / "another.sock"),
                },
            )
            first = PaddleLocalOCR.from_profile(
                first_profile,
                reading_direction="horizontal",
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            second = PaddleLocalOCR.from_profile(
                second_profile,
                reading_direction="horizontal",
                dpi=300,
                max_image_side=3000,
                jpeg_quality=90,
            )
            self.assertEqual(first.service_identity, second.service_identity)
            self.assertNotEqual(first.ocr_model, second.ocr_model)
            first_path = first._write_service_config()
            second_path = second._write_service_config()
            try:
                self.assertNotEqual(first_path, second_path)
                self.assertNotEqual(
                    json.loads(first_path.read_text(encoding="utf-8"))["socket_path"],
                    json.loads(second_path.read_text(encoding="utf-8"))["socket_path"],
                )
            finally:
                first._cleanup_service_config()
                second._cleanup_service_config()

    def test_default_spool_and_socket_runtime_directories_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_dir = root / "socket-runtime"
            profile = replace(
                self.profile(root),
                base_url=f"unix://{socket_dir / 'paddle.sock'}",
                runtime={
                    **dict(self.profile(root).runtime),
                    "auto_start": True,
                },
            )
            backend = PaddleLocalOCR.from_profile(
                profile,
                reading_direction="horizontal",
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )

            # Stop after the startup lock has created the socket parent; no
            # subprocess or real Paddle worker is needed for this regression.
            with patch.object(
                backend,
                "health",
                side_effect=[
                    {"ready": False},
                    {"ready": False},
                    {"ready": True},
                ],
            ):
                backend._ensure_service()

            self.assertEqual(
                {
                    "spool": backend.spool_dir.stat().st_mode & 0o777,
                    "socket": socket_dir.stat().st_mode & 0o777,
                },
                {"spool": 0o700, "socket": 0o700},
            )

    def test_startup_lock_refuses_a_preexisting_symlink_without_touching_victim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_dir = root / "socket-runtime"
            socket_dir.mkdir(mode=0o700)
            profile = replace(
                self.profile(root),
                base_url=f"unix://{socket_dir / 'paddle.sock'}",
                runtime={
                    **dict(self.profile(root).runtime),
                    "auto_start": True,
                },
            )
            backend = PaddleLocalOCR.from_profile(
                profile,
                reading_direction="horizontal",
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            victim = root / "startup-victim.txt"
            victim.write_text("do-not-touch", encoding="utf-8")
            startup_lock = socket_dir / ".paddle.sock.startup.lock"
            startup_lock.symlink_to(victim)

            caught: BaseException | None = None
            try:
                with patch.object(
                    backend,
                    "health",
                    side_effect=[
                        {"ready": False},
                        {"ready": False},
                        {"ready": True},
                    ],
                ):
                    backend._ensure_service()
            except (OSError, RuntimeError) as exc:
                caught = exc

            self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-touch")
            self.assertIsNotNone(
                caught,
                "a preexisting startup-lock symlink must be rejected",
            )

    def test_service_log_refuses_a_preexisting_symlink_without_touching_victim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_dir = root / "socket-runtime"
            socket_dir.mkdir(mode=0o700)
            profile = replace(
                self.profile(root),
                base_url=f"unix://{socket_dir / 'paddle.sock'}",
                runtime={
                    **dict(self.profile(root).runtime),
                    "auto_start": True,
                },
            )
            backend = PaddleLocalOCR.from_profile(
                profile,
                reading_direction="horizontal",
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
            )
            victim = root / "log-victim.txt"
            victim.write_text("do-not-touch", encoding="utf-8")
            (backend.spool_dir / "paddleocr-service.log").symlink_to(victim)

            fake_process = MagicMock()
            fake_process.poll.return_value = None

            def fake_popen(*_args, **kwargs):
                # Model the first Paddle log write. A safe implementation must
                # reject the symlink before reaching this subprocess boundary.
                kwargs["stdout"].write("attacker-controlled-append\n")
                kwargs["stdout"].flush()
                return fake_process

            caught: BaseException | None = None
            try:
                with (
                    patch.object(
                        backend,
                        "health",
                        side_effect=[
                            {"ready": False},
                            {"ready": False},
                            {"ready": False},
                            {"ready": True},
                        ],
                    ),
                    patch(
                        "ocr_backends.paddle_local.subprocess.Popen",
                        side_effect=fake_popen,
                    ),
                ):
                    backend._ensure_service()
            except (OSError, RuntimeError) as exc:
                caught = exc

            self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-touch")
            self.assertIsNotNone(
                caught,
                "a preexisting service-log symlink must be rejected",
            )

    def test_backend_is_registered_without_importing_paddle(self) -> None:
        self.assertIn("paddleocr-local", registered_backends())

    def test_package_import_registers_local_backend_in_fresh_process(self) -> None:
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import ocr_backends; print(','.join(ocr_backends.registered_backends()))",
            ],
            text=True,
        )
        self.assertIn("paddleocr-local", output.splitlines())

    def test_pipeline_kwargs_separate_content_and_runtime(self) -> None:
        kwargs = _pipeline_kwargs(
            {
                "text_detection_model": "det",
                "text_recognition_model": "rec",
                "reading_order_version": "ignored-by-paddle",
            },
            {"text_recognition_batch_size": 32, "queue_depth": 99},
            "gpu:1",
        )
        self.assertEqual(kwargs["device"], "gpu:1")
        self.assertEqual(kwargs["text_detection_model_name"], "det")
        self.assertEqual(kwargs["text_recognition_batch_size"], 32)
        self.assertNotIn("queue_depth", kwargs)
        with self.assertRaisesRegex(ValueError, "unknown paddleocr-local content"):
            _pipeline_kwargs(
                {"text_recogniton_model": "typo"},
                {},
                "gpu:0",
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _pipeline_kwargs(
                {},
                {"instances_per_device": 1.9},
                "gpu:0",
            )
        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            _pipeline_kwargs(
                {},
                {"devices": ["gpu:0", "gpu:0"]},
                "gpu:0",
            )
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            _pipeline_kwargs(
                {},
                {"worker_startup_timeout": 601},
                "gpu:0",
            )
        with self.assertRaisesRegex(ValueError, "unknown paddleocr-local runtime"):
            _pipeline_kwargs(
                {},
                {"service_module": "custom.ocr.service"},
                "gpu:0",
            )

    def test_extract_page_orders_paddle_json(self) -> None:
        prediction = {
            "res": {
                "rec_texts": ["第二行", "第一行"],
                "rec_scores": [0.9, 0.95],
                "rec_polys": [
                    [[0, 20], [20, 20], [20, 30], [0, 30]],
                    [[0, 0], [20, 0], [20, 10], [0, 10]],
                ],
            }
        }
        text, metadata = _extract_page(
            prediction,
            reading_direction="horizontal",
            horizontal_columns=1,
        )
        self.assertEqual(text, "第一行\n第二行")
        self.assertEqual(metadata["line_count"], 2)

    def test_extract_page_handles_numpy_arrays_and_true_blank_page(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is unavailable")
        prediction = {
            "res": {
                "rec_texts": np.array(["正文"]),
                "rec_scores": np.array([0.95]),
                "rec_polys": np.array([[[0, 0], [20, 0], [20, 10], [0, 10]]]),
            }
        }
        text, _ = _extract_page(
            prediction,
            reading_direction="horizontal",
            horizontal_columns=1,
        )
        self.assertEqual(text, "正文")
        blank, metadata = _extract_page(
            {"res": {"rec_texts": [], "rec_scores": [], "dt_polys": []}},
            reading_direction="horizontal",
            horizontal_columns=1,
        )
        self.assertEqual(blank, "[空白页]")
        self.assertTrue(metadata["blank_page"])


class ProtocolTests(unittest.TestCase):
    def test_json_line_protocol_round_trip(self) -> None:
        left, right = socket.socketpair()
        try:
            send_message(left, {"ok": True, "text": "中文"})
            self.assertEqual(receive_message(right)["text"], "中文")
        finally:
            left.close()
            right.close()

    def test_service_error_response_keeps_model_identity(self) -> None:
        left, right = socket.socketpair()
        server = MagicMock()
        server.config.model_identity = "paddle-test-identity"
        try:
            send_message(
                left,
                {"op": "ocr", "image_path": "/definitely/missing/page.jpg"},
            )
            _RequestHandler(right, None, server)
            response = receive_message(left)
        finally:
            left.close()
            right.close()
        self.assertFalse(response["ok"])
        self.assertIn("FileNotFoundError", response["error"])
        self.assertEqual(response["model_identity"], "paddle-test-identity")

    def test_request_retries_connect_eagain_with_one_deadline(self) -> None:
        first = MagicMock()
        first.__enter__.return_value = first
        first.connect.side_effect = BlockingIOError(
            errno.EAGAIN,
            "listen backlog is full",
        )
        second = MagicMock()
        second.__enter__.return_value = second
        with (
            patch(
                "local_ocr.protocol.socket.socket",
                side_effect=[first, second],
            ) as socket_factory,
            patch("local_ocr.protocol.send_message") as sender,
            patch(
                "local_ocr.protocol.receive_message",
                return_value={"ok": True},
            ) as receiver,
            patch("local_ocr.protocol.time.sleep") as sleeper,
        ):
            response = protocol_request(
                Path("/tmp/test-paddle.sock"),
                {"op": "health"},
                timeout=1.0,
            )
        self.assertEqual(response, {"ok": True})
        self.assertEqual(socket_factory.call_count, 2)
        sleeper.assert_called_once()
        sender.assert_called_once_with(second, {"op": "health"})
        receiver.assert_called_once_with(second)

    def test_request_connect_eagain_expires_without_unbounded_retry(self) -> None:
        failing = MagicMock()
        failing.__enter__.return_value = failing
        failing.connect.side_effect = BlockingIOError(errno.EAGAIN, "busy")
        with (
            patch("local_ocr.protocol.socket.socket", return_value=failing) as factory,
            patch(
                "local_ocr.protocol.time.monotonic",
                side_effect=[0.0, 0.1, 1.1],
            ),
            patch("local_ocr.protocol.time.sleep") as sleeper,
        ):
            with self.assertRaisesRegex(TimeoutError, "timed out communicating"):
                protocol_request(
                    Path("/tmp/test-paddle.sock"),
                    {"op": "health"},
                    timeout=1.0,
                )
        factory.assert_called_once()
        sleeper.assert_not_called()

    def test_request_does_not_retry_other_connect_errors(self) -> None:
        failing = MagicMock()
        failing.__enter__.return_value = failing
        failing.connect.side_effect = BlockingIOError(errno.EINPROGRESS, "busy")
        with patch(
            "local_ocr.protocol.socket.socket",
            return_value=failing,
        ) as factory:
            with self.assertRaises(BlockingIOError):
                protocol_request(
                    Path("/tmp/test-paddle.sock"),
                    {"op": "health"},
                    timeout=1.0,
                )
        factory.assert_called_once()

    def test_runtime_lock_is_exclusive_and_refuses_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "gpu.lock"
            first = _acquire_runtime_lock(
                lock_path,
                metadata={"owner": 1},
                busy_message="already reserved",
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "already reserved"):
                    _acquire_runtime_lock(
                        lock_path,
                        metadata={"owner": 2},
                        busy_message="already reserved",
                    )
            finally:
                _release_runtime_lock(first)

            victim = root / "victim.txt"
            victim.write_text("do-not-touch", encoding="utf-8")
            lock_path.unlink()
            lock_path.symlink_to(victim)
            with self.assertRaisesRegex(RuntimeError, "unable to open"):
                _acquire_runtime_lock(
                    lock_path,
                    metadata={"owner": 3},
                    busy_message="already reserved",
                )
            self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-touch")

    def test_shared_tmp_directory_is_never_repermissioned(self) -> None:
        shared = Path(tempfile.gettempdir())
        before = shared.stat().st_mode & 0o7777
        with self.assertRaisesRegex(RuntimeError, "shared system directory"):
            ensure_private_directory(shared)
        self.assertEqual(shared.stat().st_mode & 0o7777, before)


class BenchmarkCleanupTests(unittest.TestCase):
    @staticmethod
    def _profile(root: Path) -> ModelProfile:
        return ModelProfile(
            name="paddle",
            adapter="paddleocr-local",
            provider="local",
            model="PP-OCRv6_medium",
            base_url=f"unix://{root / 'paddle.sock'}",
            reading_direction="horizontal",
            content={},
            runtime={"devices": ["gpu:0"], "persistent": False},
        )

    def _benchmark_context(self, root: Path, client: MagicMock):
        from deploy.paddleocr import benchmark

        pdf = root / "book.pdf"
        pdf.write_bytes(b"fake")
        staging = root / "staging"
        profiles = MagicMock()
        profiles.for_stage.return_value = self._profile(root)
        document = MagicMock()
        document.__enter__.return_value.page_count = 1

        def render(_pdf, _page, image_path, **_kwargs):
            Path(image_path).write_bytes(b"image")

        patches = (
            patch.object(benchmark.fitz, "open", return_value=document),
            patch.object(benchmark, "load_pipeline_profiles", return_value=profiles),
            patch.object(
                benchmark.PaddleLocalOCR,
                "from_profile",
                return_value=client,
            ),
            patch.object(benchmark, "render_pdf_page", side_effect=render),
            patch.object(benchmark.tempfile, "mkdtemp", return_value=str(staging)),
        )
        return benchmark, pdf, staging, patches

    def test_benchmark_closes_client_when_warmup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = MagicMock()
            client.ocr_image.side_effect = RuntimeError("warmup failed")
            benchmark, pdf, staging, patches = self._benchmark_context(root, client)
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                self.assertRaisesRegex(RuntimeError, "warmup failed"),
            ):
                benchmark.main([str(pdf), "--pages", "1", "--warmup", "1"])
            client.close.assert_called_once_with()
            self.assertFalse(staging.exists())

    def test_benchmark_defaults_match_pipeline_and_report_render_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = MagicMock()
            client.ocr_model = "paddle-checkpoint"
            client.service_identity = "paddle-service"
            client.ocr_image.return_value = ("正文", "request")
            client.health.return_value = {"ready": True}
            benchmark, pdf, staging, patches = self._benchmark_context(root, client)
            stdout = io.StringIO()
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                redirect_stdout(stdout),
            ):
                self.assertEqual(
                    benchmark.main([str(pdf), "--pages", "1", "--warmup", "0"]),
                    0,
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["render"],
                {
                    "dpi": 200,
                    "max_image_side": 3000,
                    "jpeg_quality": 90,
                    "workers": 8,
                    "seconds": payload["render"]["seconds"],
                    "pages_per_second": payload["render"]["pages_per_second"],
                },
            )
            client.close.assert_called_once_with()
            self.assertFalse(staging.exists())

    def test_benchmark_removes_staging_when_close_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = MagicMock()
            client.ocr_model = "paddle-test"
            client.ocr_image.return_value = ("正文", "request")
            client.health.return_value = {"ready": True}
            client.close.side_effect = RuntimeError("close failed")
            benchmark, pdf, staging, patches = self._benchmark_context(root, client)
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "close failed"),
            ):
                benchmark.main([str(pdf), "--pages", "1", "--warmup", "0"])
            client.close.assert_called_once_with()
            self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
