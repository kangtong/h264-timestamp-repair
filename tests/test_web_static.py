from __future__ import annotations

import unittest
from pathlib import Path
import re
import sys


WEB_ROOT = Path(__file__).resolve().parents[1] / "app" / "web"
sys.path.insert(0, str(WEB_ROOT.parent))
from web_i18n import UI  # noqa: E402


class StaticWebTests(unittest.TestCase):
    def test_chinese_menu_and_manual_controls_are_present(self) -> None:
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        for label in ("概览", "问题文件", "全部文件", "任务中心", "运行设置"):
            self.assertIn(label, html)
        for control in ("立即扫描变化", "重新检测", "无损修复", "重试失败任务"):
            self.assertIn(control, html)

    def test_mobile_styles_prevent_document_overflow(self) -> None:
        css = (WEB_ROOT / "style.css").read_text(encoding="utf-8")
        self.assertIn("overflow-x:hidden", css)
        self.assertIn("@media(max-width:760px)", css)
        self.assertIn("overflow-wrap:anywhere", css)

    def test_client_uses_real_progress_and_fifty_file_page(self) -> None:
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('heartbeat.overall_progress', script)
        self.assertIn('new URLSearchParams({limit: "50"})', script)
        self.assertNotIn('active ? "45%"', script)

    def test_language_detection_switch_and_persistence_are_present(self) -> None:
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("navigator.languages", script)
        self.assertIn("localStorage.getItem(LANGUAGE_KEY)", script)
        self.assertIn("localStorage.setItem(LANGUAGE_KEY", script)
        self.assertIn('document.documentElement.lang = currentLocale', script)
        self.assertIn('$("language-select").addEventListener("change"', script)

    def test_all_static_translation_keys_exist_in_both_catalogs(self) -> None:
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        keys = set(re.findall(r'data-i18n(?:-placeholder|-aria)?="([^"]+)"', html))
        self.assertTrue(keys)
        self.assertEqual(set(UI["zh-CN"]), set(UI["en"]))
        self.assertEqual(set(), keys - set(UI["zh-CN"]))


if __name__ == "__main__":
    unittest.main()
