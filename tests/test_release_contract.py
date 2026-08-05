from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_bilingual_readmes_have_matching_configuration(self) -> None:
        chinese = (ROOT / "README.md").read_text(encoding="utf-8")
        english = (ROOT / "README.en.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/kangtong/h264-timestamp-repair/blob/main/README.en.md", chinese)
        self.assertIn("https://github.com/kangtong/h264-timestamp-repair/blob/main/README.md", english)
        variables = lambda text: set(re.findall(r"\| `([A-Z][A-Z0-9_]+)` \|", text))
        self.assertEqual(variables(chinese), variables(english))
        self.assertGreaterEqual(len(variables(chinese)), 10)
        self.assertEqual(chinese.count("\n## "), english.count("\n## "))

    def test_only_generic_compose_files_are_public(self) -> None:
        self.assertEqual(
            {"docker-compose.yml", "docker-compose.hub.yml"},
            {path.name for path in ROOT.glob("docker-compose*.yml")},
        )

    def test_public_tree_has_no_private_integration_material(self) -> None:
        forbidden = (("watch" + "cow"), chr(0x98DE) + chr(0x725B))
        searchable = {".md", ".py", ".js", ".html", ".css", ".yml", ".yaml", ".sh", ".example"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.suffix not in searchable:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore").lower()
            for term in forbidden:
                self.assertNotIn(term.lower(), content, str(path.relative_to(ROOT)))

    def test_release_version_is_consistent(self) -> None:
        for relative in (
            "Dockerfile", "docker-compose.yml", "docker-compose.hub.yml",
            "publish-to-dockerhub.sh", "app/repair_service.py", "app/web_ui.py",
        ):
            content = (ROOT / relative).read_text(encoding="utf-8")
            if relative != "Dockerfile":
                self.assertIn("3.1.0", content, relative)


if __name__ == "__main__":
    unittest.main()
