from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CollectorUIContractTests(unittest.TestCase):
    def test_collector_app_defaults_to_the_local_web_launcher(self) -> None:
        source = (
            ROOT / "macos/Sources/InnoCollectorApp/InnoCollectorApp.swift"
        ).read_text(encoding="utf-8")

        self.assertIn("LocalWebLauncher", source)
        self.assertIn('Window("英诺资讯采集", id: "collector")', source)
        self.assertNotIn("WindowGroup", source)
        self.assertNotIn(".onDisappear", source)
        self.assertNotIn("INNO_COLLECTOR_WEB_PREVIEW", source)
        self.assertNotIn("CollectorContentView", source)
        self.assertNotIn("CollectorViewModel", source)
        self.assertNotIn("MooreLocalLoginServer", source)

    def test_legacy_native_collector_sources_are_deleted(self) -> None:
        legacy = (
            "macos/Sources/InnoCollectorFeature/CollectorContentView.swift",
            "macos/Sources/InnoCollectorFeature/CollectorViewModel.swift",
            "macos/Sources/InnoCollectorFeature/MooreLocalLoginServer.swift",
            "macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift",
            "macos/Tests/InnoCollectorAppTests/MooreLocalLoginServerTests.swift",
        )

        self.assertEqual(
            [path for path in legacy if (ROOT / path).exists()],
            [],
        )


if __name__ == "__main__":
    unittest.main()
