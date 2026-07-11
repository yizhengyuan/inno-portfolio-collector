from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_CONTROL_PLANE = """on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}
"""
EXPECTED_JOB_IDS = ["repository-policy", "python-tests", "swift-tests"]
JOB_HEADER = re.compile(r"^  ([a-z][a-z0-9-]*):\n", re.MULTILINE)
FORBIDDEN_CONTEXT = re.compile(r"pull_request_target|secrets(?:\.|\s*\[)")


def _job_blocks(workflow: str) -> tuple[list[str], dict[str, str]]:
    _, separator, jobs_text = workflow.partition("\njobs:\n")
    if not separator:
        return [], {}

    headers = list(JOB_HEADER.finditer(jobs_text))
    job_ids = [header.group(1) for header in headers]
    blocks = {
        header.group(1): jobs_text[
            header.start() : headers[index + 1].start()
            if index + 1 < len(headers)
            else len(jobs_text)
        ]
        for index, header in enumerate(headers)
    }
    return job_ids, blocks


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.job_ids, cls.jobs = _job_blocks(cls.workflow)

    def assert_job_contains(self, job_id: str, *fragments: str) -> None:
        self.assertIn(job_id, self.jobs)
        block = self.jobs[job_id]
        for fragment in fragments:
            with self.subTest(job_id=job_id, fragment=fragment):
                self.assertIn(fragment, block)

    def test_control_plane_is_exact_and_read_only(self) -> None:
        start = self.workflow.index("on:\n")
        end = self.workflow.index("\njobs:\n", start)

        self.assertEqual(self.workflow[start:end], EXPECTED_CONTROL_PLANE)

    def test_job_ids_are_exact(self) -> None:
        self.assertEqual(self.job_ids, EXPECTED_JOB_IDS)

    def test_repository_policy_job_contract(self) -> None:
        self.assert_job_contains(
            "repository-policy",
            "runs-on: ubuntu-24.04",
            "timeout-minutes: 10",
            "actions/checkout@v7",
            "python3 scripts/check_repository_policy.py",
        )

    def test_python_job_contract(self) -> None:
        self.assert_job_contains(
            "python-tests",
            "runs-on: macos-15",
            "timeout-minutes: 15",
            "actions/checkout@v7",
            "actions/setup-python@v6",
            'python-version: "3.11"',
            "python -m pip install -e .",
            "python -m unittest discover -s tests",
        )

    def test_swift_job_contract(self) -> None:
        self.assert_job_contains(
            "swift-tests",
            "runs-on: macos-15",
            "timeout-minutes: 15",
            "DEVELOPER_DIR: /Applications/Xcode_16.4.app/Contents/Developer",
            "actions/checkout@v7",
            "xcodebuild -version",
            "swift --version",
            "./scripts/test_swift.sh",
        )

    def test_privileged_events_and_secret_contexts_are_forbidden(self) -> None:
        self.assertNotRegex(self.workflow, FORBIDDEN_CONTEXT)


if __name__ == "__main__":
    unittest.main()
