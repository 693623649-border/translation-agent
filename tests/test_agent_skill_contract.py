from pathlib import Path
import unittest


class AgentSkillContractTests(unittest.TestCase):
    def test_pdf_translation_skill_routes_word_rework_to_finisher(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (
            root / "skills" / "pdf-translation-pipeline" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(skill.count("$docx-publication-finisher"), 1)

    def test_word_recipes_remain_bound_to_verified_word_report(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for recipe_name in ("chinese-pdf-word.toml", "outline-word.toml"):
            with self.subTest(recipe=recipe_name):
                recipe = (root / "recipes" / recipe_name).read_text(
                    encoding="utf-8"
                )
                self.assertIn('targets = ["publication.word_report"]', recipe)
                self.assertNotIn("core.publication.verify", recipe)
                self.assertNotIn("core.publication.verify.word", recipe)

    def test_docx_finisher_keeps_reader_edition_note_and_cleanup_defaults(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (
            root / "skills" / "docx-publication-finisher" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("prune-long-footnotes", skill)
        self.assertIn("--minimum-characters 150", skill)
        self.assertIn("--remove-standalone-page-markers", skill)
        self.assertIn(".codex-trash/outputs-cleanup-*", skill)

if __name__ == "__main__":
    unittest.main()
