from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "src" / "inno_collector" / "web" / "assets"


class WebAssetContractTests(unittest.TestCase):
    def test_page_contains_all_approved_sections_and_no_script_fallback(self) -> None:
        html = (ASSETS / "index.html").read_text(encoding="utf-8")

        for label in (
            "首页",
            "登录与预检",
            "采集",
            "资料库",
            "交付",
            "稿件收件箱",
            "关于与许可证",
        ):
            self.assertIn(label, html)
        self.assertIn("<noscript>", html)
        self.assertIn("需要启用 JavaScript", html)

    def test_first_load_uses_only_same_origin_bootstrap(self) -> None:
        javascript = (ASSETS / "app.js").read_text(encoding="utf-8")

        self.assertEqual(javascript.count("fetch("), 1)
        self.assertIn('api("/api/bootstrap")', javascript)
        self.assertIn("bootstrap();", javascript)
        self.assertNotIn("http://", javascript)
        self.assertNotIn("https://", javascript)
        self.assertNotIn("//cdn", javascript)

    def test_write_buttons_start_disabled(self) -> None:
        html = (ASSETS / "index.html").read_text(encoding="utf-8")

        write_buttons = [
            line for line in html.splitlines() if "data-write-action" in line
        ]
        self.assertGreaterEqual(len(write_buttons), 4)
        self.assertTrue(all("disabled" in line for line in write_buttons))

    def test_assets_have_no_remote_dependencies_or_fonts(self) -> None:
        combined = "\n".join(
            (ASSETS / name).read_text(encoding="utf-8")
            for name in ("index.html", "app.css", "app.js")
        )

        self.assertNotIn("https://", combined)
        self.assertNotIn("http://", combined)
        self.assertNotIn("@import", combined)
        self.assertNotIn("url(", combined)

    def test_hidden_dynamic_regions_are_not_overridden_by_layout_css(self) -> None:
        css = (ASSETS / "app.css").read_text(encoding="utf-8")

        self.assertIn("[hidden] { display: none !important; }", css)

    def test_package_data_declares_web_assets(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('"inno_collector.web" = ["assets/*", "resources/*"]', pyproject)


if __name__ == "__main__":
    unittest.main()
