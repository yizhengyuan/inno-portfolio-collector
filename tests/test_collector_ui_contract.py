from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CollectorUIContractTests(unittest.TestCase):
    def test_sidebar_rows_have_explicit_selection_tags(self) -> None:
        source = (
            ROOT / "macos/Sources/InnoCollectorFeature/CollectorContentView.swift"
        ).read_text(encoding="utf-8")

        sidebar = re.search(
            r"List\(Section\.allCases, selection: \$selection\) \{ section in(?P<body>.*?)\n\s*\}",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(sidebar)
        self.assertIn(".tag(section)", sidebar.group("body"))


if __name__ == "__main__":
    unittest.main()
