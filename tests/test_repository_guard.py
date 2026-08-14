from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repository_guard import scan_repository


class RepositoryGuardTests(unittest.TestCase):
    def test_rejects_forbidden_paths_large_files_and_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe.py").write_text("print('safe')\n", encoding="utf-8")
            (root / "secret.txt").write_text(
                "token=" + "sk-" + "abcdefghijklmnopqrstuvwxyz123456",
                encoding="utf-8",
            )
            (root / "large.bin").write_bytes(b"x" * 120)
            issues = scan_repository(
                root,
                ("safe.py", "secret.txt", "large.bin", "work/book.md"),
                max_bytes=100,
            )

        self.assertEqual(
            {issue.code for issue in issues},
            {
                "possible_secret",
                "tracked_file_too_large",
                "forbidden_tracked_path",
            },
        )

    def test_clean_files_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            self.assertEqual(scan_repository(root, ("README.md",)), ())


if __name__ == "__main__":
    unittest.main()
