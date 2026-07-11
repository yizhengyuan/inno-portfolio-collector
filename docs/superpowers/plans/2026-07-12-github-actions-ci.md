# GitHub Actions CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a least-privilege GitHub Actions workflow that runs repository-policy, Python, and Swift checks on every main push and pull request.

**Architecture:** A standard-library Python policy checker audits only Git-tracked paths and reports rule names without echoing secrets. Python and Swift tests run as separate macOS jobs, while the lightweight policy job runs on Ubuntu. The Swift test launcher selects CLT-only framework flags from `DEVELOPER_DIR` so a full Xcode runner never mixes toolchains.

**Tech Stack:** Python 3.11 `unittest`, zsh, SwiftPM/Swift Testing, GitHub Actions, `macos-15`, Xcode 16.4, `ubuntu-24.04`.

---

## File map

- Create `scripts/check_repository_policy.py`: deterministic audit of Git-tracked paths, required notices, forbidden filenames, and high-confidence credential formats.
- Create `tests/test_repository_policy.py`: unit tests for all policy rules and secret-safe diagnostics.
- Create `tests/test_swift_test_script.py`: subprocess regression tests for CLT and full-Xcode command construction.
- Modify `scripts/test_swift.sh`: derive special Swift Testing paths only when the selected developer directory is CommandLineTools.
- Create `.github/workflows/ci.yml`: three parallel, read-only CI jobs.
- Create `tests/test_ci_workflow.py`: dependency-free assertions for workflow triggers, permissions, runners, commands, and absence of secret contexts.

### Task 1: Repository policy checker

**Files:**
- Create: `tests/test_repository_policy.py`
- Create: `scripts/check_repository_policy.py`

- [ ] **Step 1: Write failing policy tests**

Create `tests/test_repository_policy.py` with cases that call `audit_repository(paths, read_bytes)` and assert:

```python
from __future__ import annotations

import unittest

from scripts.check_repository_policy import PolicyViolation, audit_repository


REQUIRED = {
    "LICENSE": b"MIT License\nPermission is hereby granted",
    "SECURITY.md": "私密漏洞报告".encode("utf-8"),
    "THIRD_PARTY_NOTICES.md": b"wechat-article-exporter moore-wechat-article-downloader",
    "third_party/licenses/wechat-article-exporter-LICENSE.txt": b"MIT License",
    "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt": b"MIT License",
}


def audit(extra: dict[str, bytes] | None = None, omitted: set[str] | None = None):
    files = dict(REQUIRED)
    files.update(extra or {})
    for path in omitted or set():
        files.pop(path, None)
    return audit_repository(sorted(files), files.__getitem__)


class RepositoryPolicyTests(unittest.TestCase):
    def test_clean_repository_passes(self) -> None:
        self.assertEqual(audit({"src/example.py": b"token=fixture-secret"}), [])

    def test_required_notice_is_reported_without_file_content(self) -> None:
        violations = audit(omitted={"LICENSE"})
        self.assertEqual(violations, [PolicyViolation("LICENSE", "required-file-missing")])

    def test_forbidden_user_material_and_credential_files_are_rejected(self) -> None:
        violations = audit({
            "英诺项目清单-2026/source.xlsx": b"fixture",
            ".superpowers/session.json": b"{}",
            ".env.production": b"SAFE=fixture",
            "certificates/distribution.p12": b"fixture",
        })
        self.assertEqual(
            [(item.path, item.rule) for item in violations],
            [
                (".env.production", "credential-file"),
                (".superpowers/session.json", "user-material"),
                ("certificates/distribution.p12", "credential-file"),
                ("英诺项目清单-2026/source.xlsx", "user-material"),
            ],
        )

    def test_high_confidence_tokens_and_private_keys_are_rejected(self) -> None:
        github = "ghp_" + "A" * 36
        aws = "AKIA" + "B" * 16
        violations = audit({
            "one.txt": github.encode(),
            "two.txt": aws.encode(),
            "three.txt": b"-----BEGIN " + b"PRIVATE KEY-----",
        })
        self.assertEqual({item.rule for item in violations}, {
            "github-token", "aws-access-key", "private-key",
        })
        self.assertNotIn(github, repr(violations))
        self.assertNotIn(aws, repr(violations))

    def test_binary_and_large_files_skip_content_scan(self) -> None:
        violations = audit({
            "binary.bin": b"\0ghp_" + b"A" * 36,
            "large.txt": b"ghp_" + b"A" * 2_100_000,
        })
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
./.venv/bin/python -m unittest tests.test_repository_policy -v
```

Expected: `ImportError` because `scripts.check_repository_policy` does not exist.

- [ ] **Step 3: Implement the minimal policy checker**

Create `scripts/check_repository_policy.py` with:

```python
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 2_000_000
REQUIRED_MARKERS = {
    "LICENSE": (b"MIT License", b"Permission is hereby granted"),
    "SECURITY.md": ("私密漏洞报告".encode("utf-8"),),
    "THIRD_PARTY_NOTICES.md": (
        b"wechat-article-exporter", b"moore-wechat-article-downloader",
    ),
    "third_party/licenses/wechat-article-exporter-LICENSE.txt": (b"MIT License",),
    "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt": (b"MIT License",),
}
USER_MATERIAL_PREFIXES = (".superpowers/", "英诺项目清单-2026/", "runtime/", ".moore/")
CREDENTIAL_NAMES = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..*)?|id_rsa|id_ed25519|credentials?|secrets?|[^/]+\.(?:pem|key|p12|pfx))$"
)
SECRET_PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github-token", re.compile(rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("aws-access-key", re.compile(rb"AKIA[0-9A-Z]{16}")),
)


@dataclass(frozen=True, order=True)
class PolicyViolation:
    path: str
    rule: str


def audit_repository(
    tracked_paths: Iterable[str],
    read_bytes: Callable[[str], bytes],
) -> list[PolicyViolation]:
    paths = sorted(set(tracked_paths))
    path_set = set(paths)
    violations: set[PolicyViolation] = set()

    for required, markers in REQUIRED_MARKERS.items():
        if required not in path_set:
            violations.add(PolicyViolation(required, "required-file-missing"))
            continue
        try:
            content = read_bytes(required)
        except OSError:
            violations.add(PolicyViolation(required, "tracked-file-unreadable"))
            continue
        if any(marker not in content for marker in markers):
            violations.add(PolicyViolation(required, "required-marker-missing"))

    for raw_path in paths:
        path = PurePosixPath(raw_path).as_posix()
        if path.startswith(USER_MATERIAL_PREFIXES):
            violations.add(PolicyViolation(path, "user-material"))
        if CREDENTIAL_NAMES.search(path):
            violations.add(PolicyViolation(path, "credential-file"))
        try:
            content = read_bytes(raw_path)
        except OSError:
            violations.add(PolicyViolation(path, "tracked-file-unreadable"))
            continue
        if len(content) > MAX_TEXT_BYTES or b"\0" in content:
            continue
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                violations.add(PolicyViolation(path, rule))

    return sorted(violations)


def tracked_paths(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    )
    return [value.decode("utf-8") for value in result.stdout.split(b"\0") if value]


def main() -> int:
    paths = tracked_paths()
    violations = audit_repository(paths, lambda path: (ROOT / path).read_bytes())
    if violations:
        for item in violations:
            print(f"{item.path}: {item.rule}")
        return 1
    print(f"repository policy passed: {len(paths)} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run focused tests and the real audit**

Run:

```bash
./.venv/bin/python -m unittest tests.test_repository_policy -v
./.venv/bin/python scripts/check_repository_policy.py
```

Expected: 5 tests pass and the audit prints `repository policy passed`.

- [ ] **Step 5: Commit the policy checker**

```bash
git add scripts/check_repository_policy.py tests/test_repository_policy.py
git commit -m "ci: enforce repository safety policy"
```

### Task 2: Swift toolchain isolation

**Files:**
- Create: `tests/test_swift_test_script.py`
- Modify: `scripts/test_swift.sh:13-25`

- [ ] **Step 1: Write failing subprocess regression tests**

Create `tests/test_swift_test_script.py`. Each test creates an executable fake `swift` at the front of `PATH`, sets `DEVELOPER_DIR`, invokes `scripts/test_swift.sh`, and inspects printed arguments. Assert a temporary path ending in `CommandLineTools` adds `-Xswiftc` and its framework path, while `Xcode.app/Contents/Developer` does not.

```python
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SwiftTestScriptTests(unittest.TestCase):
    def invoke(self, developer_dir: Path, create_framework: bool) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            swift = bin_dir / "swift"
            swift.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            swift.chmod(swift.stat().st_mode | stat.S_IXUSR)
            if create_framework:
                (developer_dir / "Library/Developer/Frameworks/Testing.framework").mkdir(parents=True)
                (developer_dir / "Library/Developer/usr/lib").mkdir(parents=True)
            result = subprocess.run(
                [str(ROOT / "scripts/test_swift.sh")],
                text=True,
                capture_output=True,
                check=True,
                env={
                    **os.environ,
                    "DEVELOPER_DIR": str(developer_dir),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                },
            )
            return result.stdout.splitlines()

    def test_command_line_tools_adds_testing_framework_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            developer = Path(temporary) / "CommandLineTools"
            arguments = self.invoke(developer, create_framework=True)
            self.assertIn("-Xswiftc", arguments)
            self.assertIn(str(developer / "Library/Developer/Frameworks"), arguments)

    def test_full_xcode_does_not_mix_command_line_tools_frameworks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            developer = Path(temporary) / "Xcode.app/Contents/Developer"
            arguments = self.invoke(developer, create_framework=False)
            self.assertNotIn("-Xswiftc", arguments)
            self.assertEqual(arguments[:3], ["test", "--enable-swift-testing", "--disable-xctest"])
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
./.venv/bin/python -m unittest tests.test_swift_test_script -v
```

Expected: the CLT test fails because the script still uses the fixed system path.

- [ ] **Step 3: Make `test_swift.sh` respect the selected toolchain**

Replace lines 13-25 with:

```zsh
DEVELOPER_ROOT="${DEVELOPER_DIR:-$(xcode-select -p)}"
FRAMEWORKS="$DEVELOPER_ROOT/Library/Developer/Frameworks"
LIBRARIES="$DEVELOPER_ROOT/Library/Developer/usr/lib"

if [[ "${DEVELOPER_ROOT:t}" == "CommandLineTools" && -d "$FRAMEWORKS/Testing.framework" ]]; then
  exec swift test --enable-swift-testing --disable-xctest \
    -Xswiftc -F -Xswiftc "$FRAMEWORKS" \
    -Xlinker "-F$FRAMEWORKS" \
    -Xlinker -rpath -Xlinker "$FRAMEWORKS" \
    -Xlinker -rpath -Xlinker "$LIBRARIES" \
    "$@"
fi

exec swift test --enable-swift-testing --disable-xctest "$@"
```

- [ ] **Step 4: Run regression and real Swift tests**

```bash
./.venv/bin/python -m unittest tests.test_swift_test_script -v
./scripts/test_swift.sh
```

Expected: 2 regression tests and all 34 Swift tests pass.

- [ ] **Step 5: Commit the Swift CI compatibility fix**

```bash
git add scripts/test_swift.sh tests/test_swift_test_script.py
git commit -m "test: isolate Swift CI toolchains"
```

### Task 3: Read-only GitHub Actions workflow

**Files:**
- Create: `tests/test_ci_workflow.py`
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write a failing workflow contract test**

Create `tests/test_ci_workflow.py` that reads `.github/workflows/ci.yml` as text and asserts the exact trigger, permission, runner, action, and command fragments below are present; also assert `secrets.` and `pull_request_target` are absent.

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CIWorkflowTests(unittest.TestCase):
    def test_ci_workflow_has_three_read_only_jobs(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for fragment in (
            "pull_request:", "workflow_dispatch:", "contents: read",
            "repository-policy:", "python-tests:", "swift-tests:",
            "runs-on: ubuntu-24.04", "runs-on: macos-15",
            "actions/checkout@v7", "actions/setup-python@v6",
            "python3 scripts/check_repository_policy.py",
            "python -m unittest discover -s tests",
            "./scripts/test_swift.sh",
            "/Applications/Xcode_16.4.app/Contents/Developer",
        ):
            self.assertIn(fragment, workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
```

- [ ] **Step 2: Run the contract test and verify RED**

```bash
./.venv/bin/python -m unittest tests.test_ci_workflow -v
```

Expected: `FileNotFoundError` because `.github/workflows/ci.yml` does not exist.

- [ ] **Step 3: Add the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
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

jobs:
  repository-policy:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
      - name: Check tracked repository content
        run: python3 scripts/check_repository_policy.py

  python-tests:
    runs-on: macos-15
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: '3.11'
      - name: Install project
        run: python -m pip install --disable-pip-version-check -e .
      - name: Run Python tests
        run: python -m unittest discover -s tests

  swift-tests:
    runs-on: macos-15
    timeout-minutes: 15
    env:
      DEVELOPER_DIR: /Applications/Xcode_16.4.app/Contents/Developer
    steps:
      - uses: actions/checkout@v7
      - name: Report Swift toolchain
        run: |
          xcodebuild -version
          swift --version
      - name: Run Swift tests
        run: ./scripts/test_swift.sh
```

- [ ] **Step 4: Run workflow contract and repository policy tests**

```bash
./.venv/bin/python -m unittest tests.test_ci_workflow tests.test_repository_policy -v
./.venv/bin/python scripts/check_repository_policy.py
```

Expected: all focused tests pass and policy prints a success summary.

- [ ] **Step 5: Commit the workflow**

```bash
git add .github/workflows/ci.yml tests/test_ci_workflow.py
git commit -m "ci: run Python Swift and policy checks"
```

### Task 4: Full and remote verification

**Files:**
- Modify only if verification exposes a defect.

- [ ] **Step 1: Run all local verification**

```bash
./.venv/bin/python -m unittest discover -s tests
./scripts/test_swift.sh
./.venv/bin/python scripts/check_repository_policy.py
git diff --check
```

Expected: 296 Python tests pass with the one frozen-helper suite skipped, 34 Swift tests pass with 3 helper-dependent tests skipped, policy passes, and `git diff --check` emits no output.

- [ ] **Step 2: Create the GitHub progress issue and feature branch**

Create an issue in milestone `v0.1 朋友试用` titled `建立 GitHub Actions 持续集成门禁`, then push branch `feat/github-actions-ci` and open a Pull Request that references the issue.

- [ ] **Step 3: Verify all three GitHub jobs reach success**

```bash
gh pr checks --watch --interval 10
```

Expected: `repository-policy`, `python-tests`, and `swift-tests` all report `pass`.

- [ ] **Step 4: Review the diff and merge**

Run the requesting-code-review workflow, resolve any findings, merge the PR, and confirm the `main` push CI run is also green.

- [ ] **Step 5: Close progress tracking**

Close the CI issue with links to the merged PR and successful `main` workflow run. Do not create a `v0.1.0` tag or GitHub Release in this task.
