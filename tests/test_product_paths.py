from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from product_paths import (
    default_profile_path,
    installed_data_root,
    recipe_paths,
    resource_root,
)


class ProductPathTests(unittest.TestCase):
    def test_source_assets_take_precedence_over_installed_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            installed = installed_data_root(root / "prefix")
            (source / "recipes").mkdir(parents=True)
            (source / "recipes" / "source.toml").write_text("", encoding="utf-8")
            (installed / "recipes").mkdir(parents=True)
            (installed / "recipes" / "installed.toml").write_text(
                "", encoding="utf-8"
            )

            resolved = resource_root(source_root=source, prefix=root / "prefix")
            recipes = recipe_paths(source_root=source, prefix=root / "prefix")

        self.assertEqual(resolved, source.resolve())
        self.assertEqual([path.name for path in recipes], ["source.toml"])

    def test_wheel_data_is_used_when_source_assets_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "empty-source"
            source.mkdir()
            installed = installed_data_root(root / "prefix")
            (installed / "recipes").mkdir(parents=True)
            (installed / "recipes" / "wheel.toml").write_text("", encoding="utf-8")
            (installed / "pipeline.example.toml").write_text(
                "schema_version=1\n", encoding="utf-8"
            )

            resolved = resource_root(source_root=source, prefix=root / "prefix")
            profile = default_profile_path(
                cwd=root / "working",
                source_root=source,
                prefix=root / "prefix",
            )

        self.assertEqual(resolved, installed.resolve())
        self.assertEqual(profile, installed.resolve() / "pipeline.example.toml")


if __name__ == "__main__":
    unittest.main()
