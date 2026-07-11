from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_safe_triggers_and_read_only_permissions_are_declared(self) -> None:
        for fragment in (
            "pull_request:",
            "workflow_dispatch:",
            "contents: read",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.workflow)

        self.assertNotIn("pull_request_target", self.workflow)
        self.assertNotIn("secrets.", self.workflow)

    def test_three_jobs_use_the_required_runners(self) -> None:
        for fragment in (
            "repository-policy:",
            "python-tests:",
            "swift-tests:",
            "ubuntu-24.04",
            "macos-15",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.workflow)

    def test_jobs_use_pinned_tools_and_project_entry_points(self) -> None:
        for fragment in (
            "actions/checkout@v7",
            "actions/setup-python@v6",
            "python3 scripts/check_repository_policy.py",
            "python -m unittest discover -s tests",
            "./scripts/test_swift.sh",
            "/Applications/Xcode_16.4.app/Contents/Developer",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.workflow)


if __name__ == "__main__":
    unittest.main()
