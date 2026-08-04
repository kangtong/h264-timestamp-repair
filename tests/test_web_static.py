from __future__ import annotations

import unittest
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "app" / "web"


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


if __name__ == "__main__":
    unittest.main()
