from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DistributionLicenseTests(unittest.TestCase):
    def test_required_mit_notices_are_vendored_verbatim(self) -> None:
        exporter = (
            ROOT / "third_party/licenses/wechat-article-exporter-LICENSE.txt"
        ).read_text(encoding="utf-8")
        moore = (
            ROOT / "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("Copyright (c) 2024 Jock", exporter)
        self.assertIn("Copyright (c) 2026 Moore-developers", moore)
        self.assertIn("Permission is hereby granted", exporter)
        self.assertIn("Permission is hereby granted", moore)
        self.assertTrue(exporter.endswith("\n"))
        self.assertTrue(moore.endswith("\n"))


class DistributionDocumentationTests(unittest.TestCase):
    def test_readme_and_manual_gate_cover_single_customer_distribution(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        checklist = (ROOT / "docs/macos-release-checklist.md").read_text(encoding="utf-8")
        for phrase in (
            "英诺资讯采集", "客户无需安装英诺专用 App", "Obsidian", "离线看板",
            "客户资料包 ZIP", "公众号登录凭据", "文章版权", "默认浏览器", "本地 Web",
            "未来多人模式",
        ):
            self.assertIn(phrase, readme)
        for phrase in (
            "macOS 13", "Gatekeeper", "Python 和 Codex", "断开网络",
            "稿件字节", "codesign", "spctl", "第三方许可证",
            "InnoCollectorWebServer",
        ):
            self.assertIn(phrase, checklist)


if __name__ == "__main__":
    unittest.main()
