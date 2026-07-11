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


if __name__ == "__main__":
    unittest.main()
