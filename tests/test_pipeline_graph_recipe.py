import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import NodeResult, NodeSpec
from pipeline_graph.recipe import (
    ENTRY_POINT_GROUP,
    MAX_RECIPE_BYTES,
    NodeRegistry,
    PluginNotAllowedError,
    PluginNotFoundError,
    PluginRegistrationError,
    RecipeSchemaError,
    RecipeSecurityError,
    load_recipe,
    parse_recipe,
)


def node(name, *, requires=(), provides=(), version="1"):
    return NodeSpec(
        name=name,
        handler=lambda _context: NodeResult(
            outputs={provided: provided for provided in provides}
        ),
        requires=frozenset(requires),
        provides=frozenset(provides),
        version=version,
    )


class _FakeEntryPoint:
    def __init__(self, name, provider, *, group=ENTRY_POINT_GROUP):
        self.name = name
        self.group = group
        self.provider = provider
        self.load_count = 0

    def load(self):
        self.load_count += 1
        return self.provider


class RecipeParsingTests(unittest.TestCase):
    def test_parses_only_the_low_code_selection_schema(self):
        recipe = parse_recipe(
            """
schema_version = 1
id = "chinese-pdf-word"
targets = ["docx"]
enable = ["acme.clean_headers"]
disable = ["core.translate"]
required_plugins = ["acme_cleanup"]
"""
        )
        self.assertEqual(recipe.id, "chinese-pdf-word")
        self.assertEqual(recipe.targets, ("docx",))
        self.assertEqual(recipe.enable, ("acme.clean_headers",))
        self.assertEqual(recipe.disable, ("core.translate",))
        self.assertEqual(recipe.required_plugins, ("acme_cleanup",))

    def test_rejects_unknown_fields_and_unsupported_schema(self):
        with self.assertRaises(RecipeSchemaError):
            parse_recipe(
                "schema_version=2\nid='book'\ntargets=['docx']\n"
            )
        with self.assertRaises(RecipeSchemaError):
            parse_recipe(
                "schema_version=1\nid='book'\ntargets=['docx']\nworkers=4\n"
            )
        with self.assertRaises(RecipeSchemaError):
            parse_recipe("schema_version=1\nid='book'\n")

    def test_rejects_every_executable_endpoint_or_secret_field(self):
        dangerous = (
            "import",
            "module",
            "callable",
            "command",
            "api_key",
            "token",
            "base_url",
            "credential_env",
            "password",
            "secret",
        )
        for field_name in dangerous:
            with self.subTest(field_name=field_name):
                with self.assertRaises(RecipeSecurityError):
                    parse_recipe(
                        {
                            "schema_version": 1,
                            "id": "book",
                            "targets": ["docx"],
                            field_name: "do-not-load-this",
                        }
                    )

    def test_rejects_import_paths_duplicates_and_conflicting_selection(self):
        unsafe_values = (
            {"enable": ["package.module:factory"]},
            {"enable": ["../../plugin"]},
            {"required_plugins": ["plugin/name"]},
        )
        for extra in unsafe_values:
            with self.subTest(extra=extra):
                with self.assertRaises(RecipeSecurityError):
                    parse_recipe(
                        {
                            "schema_version": 1,
                            "id": "book",
                            "targets": ["docx"],
                            **extra,
                        }
                    )
        with self.assertRaises(RecipeSchemaError):
            parse_recipe(
                {
                    "schema_version": 1,
                    "id": "book",
                    "targets": ["docx", "docx"],
                }
            )
        with self.assertRaises(RecipeSchemaError):
            parse_recipe(
                {
                    "schema_version": 1,
                    "id": "book",
                    "targets": ["docx"],
                    "enable": ["core.ocr"],
                    "disable": ["core.ocr"],
                }
            )

    def test_load_recipe_enforces_a_small_data_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.toml"
            valid.write_text(
                "schema_version=1\nid='book'\ntargets=['docx']\n",
                encoding="utf-8",
            )
            self.assertEqual(load_recipe(valid).id, "book")

            oversized = Path(directory) / "oversized.toml"
            oversized.write_bytes(b"x" * (MAX_RECIPE_BYTES + 1))
            with self.assertRaises(RecipeSchemaError):
                load_recipe(oversized)

    def test_builtin_word_recipes_target_verified_word_report(self):
        recipe_root = Path(__file__).resolve().parents[1] / "recipes"
        expected_enable = {
            "chinese-pdf-word.toml": (),
            "outline-word.toml": ("core.toc.from_outline",),
        }
        disabled_publishers = {
            "core.publish.knowledge_base",
            "core.publish.epub",
            "core.publish.reference_pdf",
        }

        for filename, enable in expected_enable.items():
            with self.subTest(recipe=filename):
                recipe = load_recipe(recipe_root / filename)
                self.assertEqual(
                    recipe.targets,
                    ("publication.word_report",),
                )
                self.assertEqual(recipe.enable, enable)
                self.assertTrue(disabled_publishers.issubset(recipe.disable))
                self.assertNotIn("core.publish.docx", recipe.disable)
                self.assertNotIn("core.publication.verify", recipe.disable)
                self.assertNotIn(
                    "core.publication.verify.word",
                    recipe.disable,
                )

    def test_text_pdf_recipe_replaces_ocr_and_keeps_full_release_gate(self):
        recipe = load_recipe(
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "text-pdf-full-publication.toml"
        )

        self.assertEqual(recipe.targets, ("publication.report",))
        self.assertEqual(recipe.enable, ("core.pages.text_extract",))
        self.assertIn("core.pages.ocr", recipe.disable)
        self.assertNotIn("core.publication.verify", recipe.disable)


class NodeRegistryTests(unittest.TestCase):
    def test_registers_and_replaces_only_trusted_core_nodes(self):
        original = node("core.ocr", provides={"pages"}, version="1")
        replacement = node("core.ocr", provides={"pages"}, version="2")
        registry = NodeRegistry([original])

        self.assertIs(registry.get("core.ocr"), original)
        self.assertIs(registry.replace_core(replacement), replacement)
        self.assertIs(registry.get("core.ocr"), replacement)
        self.assertEqual(registry.origins["core.ocr"], "core")

        with self.assertRaises(KeyError):
            registry.replace_core(node("core.missing"))
        with self.assertRaises(ValueError):
            registry.register_core(node("not_core"))

    def test_recipe_selects_defaults_then_enable_and_disable(self):
        registry = NodeRegistry()
        registry.register_core(node("core.ocr", requires={"pdf"}, provides={"pages"}))
        registry.register_core(
            node("core.translate", requires={"pages"}, provides={"translated"})
        )
        registry.register_core(
            node("core.experimental", provides={"experimental"}),
            enabled_by_default=False,
        )
        recipe = parse_recipe(
            {
                "schema_version": 1,
                "id": "book",
                "targets": ["experimental"],
                "enable": ["core.experimental"],
                "disable": ["core.translate"],
            }
        )

        graph = registry.build_graph(recipe)
        self.assertEqual(
            tuple(spec.name for spec in graph.nodes),
            ("core.ocr", "core.experimental"),
        )
        self.assertEqual(
            tuple(spec.name for spec in graph.plan(available={"pdf"}, targets=recipe.targets)),
            ("core.experimental",),
        )

    def test_external_plugin_requires_allowlist_and_is_opt_in(self):
        external = node("acme.clean_headers", requires={"pages"}, provides={"clean_pages"})

        def register_nodes(registrar):
            registrar.register(external)

        entry_point = _FakeEntryPoint("acme_cleanup", register_nodes)
        registry = NodeRegistry(
            [node("core.ocr", requires={"pdf"}, provides={"pages"})]
        )
        recipe = parse_recipe(
            {
                "schema_version": 1,
                "id": "book",
                "targets": ["clean_pages"],
                "enable": ["acme.clean_headers"],
                "required_plugins": ["acme_cleanup"],
            }
        )

        with patch(
            "pipeline_graph.recipe.importlib_metadata.entry_points",
            return_value=(entry_point,),
        ) as discover:
            with self.assertRaises(PluginNotAllowedError):
                registry.prepare_graph(recipe)
            self.assertEqual(entry_point.load_count, 0)
            discover.assert_not_called()

            graph = registry.prepare_graph(
                recipe,
                plugin_allowlist=("acme_cleanup",),
            )

        self.assertEqual(entry_point.load_count, 1)
        self.assertEqual(registry.loaded_plugins, frozenset({"acme_cleanup"}))
        self.assertEqual(registry.origins["acme.clean_headers"], "plugin:acme_cleanup")
        self.assertEqual(
            tuple(spec.name for spec in graph.plan(available={"pdf"}, targets=recipe.targets)),
            ("core.ocr", "acme.clean_headers"),
        )

    def test_unknown_or_wrong_group_plugin_is_not_imported(self):
        wrong_group = _FakeEntryPoint(
            "acme_cleanup",
            lambda _registrar: None,
            group="unrelated.group",
        )
        registry = NodeRegistry()
        with patch(
            "pipeline_graph.recipe.importlib_metadata.entry_points",
            return_value=(wrong_group,),
        ):
            with self.assertRaises(PluginNotFoundError):
                registry.load_plugins(
                    ("acme_cleanup",),
                    allowlist=("acme_cleanup",),
                )
        self.assertEqual(wrong_group.load_count, 0)

    def test_external_plugin_cannot_override_core_and_registration_rolls_back(self):
        core = node("core.ocr", provides={"pages"})
        external_before_failure = node("acme.partial", provides={"partial"})

        def malicious_registration(registrar):
            registrar.register(external_before_failure)
            registrar.register(node("core.ocr", provides={"compromised"}))

        entry_point = _FakeEntryPoint("malicious", malicious_registration)
        registry = NodeRegistry([core])
        with patch(
            "pipeline_graph.recipe.importlib_metadata.entry_points",
            return_value=(entry_point,),
        ):
            with self.assertRaises(PluginRegistrationError):
                registry.load_plugins(
                    ("malicious",),
                    allowlist=("malicious",),
                )

        self.assertEqual(registry.nodes, (core,))
        self.assertEqual(registry.loaded_plugins, frozenset())

    def test_recipe_dependency_does_not_grant_plugin_permission(self):
        registry = NodeRegistry()
        recipe = parse_recipe(
            {
                "schema_version": 1,
                "id": "book",
                "targets": ["docx"],
                "required_plugins": ["declared_is_not_allowed"],
            }
        )
        with self.assertRaises(PluginNotAllowedError):
            registry.build_graph(recipe)


if __name__ == "__main__":
    unittest.main()
