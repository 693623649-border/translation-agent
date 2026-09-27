from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"


def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", text)
    if not match:
        return ""
    return match.group(1).strip().strip('"\'')


class ProjectSkillContractTests(unittest.TestCase):
    def test_project_skills_are_discoverable_and_self_contained(self) -> None:
        skill_dirs = sorted(path for path in SKILLS_ROOT.iterdir() if path.is_dir())
        self.assertTrue(skill_dirs, "the project must expose at least one skill")

        for skill_dir in skill_dirs:
            with self.subTest(skill=skill_dir.name):
                skill_path = skill_dir / "SKILL.md"
                agent_path = skill_dir / "agents" / "openai.yaml"
                self.assertTrue(skill_path.is_file())
                self.assertTrue(agent_path.is_file())

                skill_text = skill_path.read_text(encoding="utf-8")
                self.assertTrue(skill_text.startswith("---\n"))
                frontmatter_end = skill_text.find("\n---\n", 4)
                self.assertGreater(frontmatter_end, 4)
                frontmatter = skill_text[4:frontmatter_end]
                self.assertEqual(_frontmatter_value(frontmatter, "name"), skill_dir.name)
                self.assertTrue(_frontmatter_value(frontmatter, "description"))

                agent_text = agent_path.read_text(encoding="utf-8")
                self.assertIn(f"${skill_dir.name}", agent_text)

                for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", skill_text):
                    if "://" in target or target.startswith("#"):
                        continue
                    resolved = (skill_dir / target).resolve()
                    self.assertTrue(resolved.is_relative_to(skill_dir.resolve()))
                    self.assertTrue(resolved.is_file(), target)


if __name__ == "__main__":
    unittest.main()
