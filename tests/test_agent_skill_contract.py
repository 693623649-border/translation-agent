from pathlib import Path
import unittest


class AgentSkillContractTests(unittest.TestCase):
    def test_pdf_translation_skill_covers_word_rework_release_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (
            root / "skills" / "pdf-translation-pipeline" / "SKILL.md"
        ).read_text(encoding="utf-8")

        required_phrases = (
            "处理 Word 返工问题",
            "原文页码数字",
            "硬换行按段落语义合并",
            "正文宋体、两端对齐、零字符间距",
            "真实 `word/footnotes.xml` 包",
            "publication.word_report",
            "word-release-report.json",
            "不要直接编辑 canonical DOCX",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

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


if __name__ == "__main__":
    unittest.main()
