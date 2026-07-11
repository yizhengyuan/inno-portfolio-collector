# macOS Distribution and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce role-isolated `.app` bundles and DMGs, preserve upstream MIT notices, support Developer ID signing/notarization, and verify the complete collector-to-reader workflow on clean macOS user accounts.

**Architecture:** SwiftPM builds the two native executables. PyInstaller builds a reader helper plus a collector helper and a separate Moore exporter sidecar; the sidecar preserves the current subprocess isolation without requiring a system Python. A deterministic Python bundler assembles each `.app` with only its role's binaries, configuration, and licenses, then signs inner components before the outer bundle. Release tooling fails closed when signing credentials are missing; ad-hoc builds remain available for local QA.

**Tech Stack:** Python 3.11, PyInstaller 6.x, SwiftPM/Swift 6.3, `codesign`, `xcrun notarytool`, `stapler`, `hdiutil`, `unittest`.

---

**Prerequisites:** Complete `docs/superpowers/plans/2026-07-11-content-package-core.md` and `docs/superpowers/plans/2026-07-11-dual-macos-apps.md`.

## File map

- Modify `pyproject.toml`: add the pinned build-only PyInstaller dependency and helper entry points.
- Create `packaging/collector_helper_entry.py`, `packaging/reader_helper_entry.py`, and `packaging/moore_exporter_entry.py`: separate frozen entry points.
- Create `packaging/Info-Collector.plist` and `packaging/Info-Reader.plist`: stable bundle metadata and document types.
- Create `packaging/collector.entitlements` and `packaging/reader.entitlements`: least-privilege hardened-runtime settings.
- Create `scripts/build_helpers.py`: build and audit role-specific helpers.
- Create `scripts/build_macos_apps.py`: compile Swift, assemble `.app` bundles, copy licenses, and ad-hoc sign.
- Create `scripts/release_macos.py`: Developer ID sign, DMG creation, notarization, stapling, and release manifest.
- Create `third_party/licenses/wechat-article-exporter-LICENSE.txt` and `third_party/licenses/moore-wechat-article-downloader-LICENSE.txt`.
- Create `THIRD_PARTY_NOTICES.md` and modify `NOTICE.md`, `README.md`, and `.gitignore`.
- Create `tests/test_build_helpers.py`, `tests/test_build_macos_apps.py`, `tests/test_release_macos.py`, and `tests/test_distribution_end_to_end.py`.

### Task 1: Vendor exact upstream notices and define attribution

**Files:**
- Create: `third_party/licenses/wechat-article-exporter-LICENSE.txt`
- Create: `third_party/licenses/moore-wechat-article-downloader-LICENSE.txt`
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `NOTICE.md`
- Create: `tests/test_distribution_end_to_end.py`

- [ ] **Step 1: Write a failing license-presence test**

```python
class DistributionLicenseTests(unittest.TestCase):
    def test_required_mit_notices_are_vendored_verbatim(self) -> None:
        exporter = (ROOT / "third_party/licenses/wechat-article-exporter-LICENSE.txt").read_text(encoding="utf-8")
        moore = (ROOT / "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2024 Jock", exporter)
        self.assertIn("Copyright (c) 2026 Moore-developers", moore)
        self.assertIn("Permission is hereby granted", exporter)
        self.assertIn("Permission is hereby granted", moore)
```

- [ ] **Step 2: Run and confirm the files are missing**

Run: `./.venv/bin/python -m unittest tests.test_distribution_end_to_end.DistributionLicenseTests -v`

Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Add the exact license texts**

Copy the complete license text from:

- `https://github.com/wechat-article/wechat-article-exporter/blob/master/LICENSE` with `Copyright (c) 2024 Jock`;
- `../moore-wechat-article-downloader/LICENSE` with `Copyright (c) 2026 Moore-developers`.

Do not paraphrase either license. `THIRD_PARTY_NOTICES.md` must identify the repository URL, MIT license path, whether code is bundled or only adapted, and the local component that uses it. State separately that article copyright remains with authors/rightsholders.

- [ ] **Step 4: Replace conditional wording in `NOTICE.md`**

State that the product adapts both projects, always ships both license texts, never ships credentials, and records future copied/modified source files in `THIRD_PARTY_NOTICES.md`.

- [ ] **Step 5: Run and commit**

Run: `./.venv/bin/python -m unittest tests.test_distribution_end_to_end.DistributionLicenseTests -v`

Expected: PASS.

```bash
git add third_party/licenses THIRD_PARTY_NOTICES.md NOTICE.md tests/test_distribution_end_to_end.py
git commit -m "docs: preserve upstream MIT notices"
```

### Task 2: Build separate frozen helper binaries

**Files:**
- Modify: `pyproject.toml`
- Create: `packaging/collector_helper_entry.py`
- Create: `packaging/reader_helper_entry.py`
- Create: `scripts/build_helpers.py`
- Create: `tests/test_build_helpers.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add a build dependency and three minimal entries**

```toml
[project.optional-dependencies]
build = ["pyinstaller>=6.0,<7.0"]

[project.scripts]
inno-collect = "inno_collector.cli:main"
inno-collector-helper = "inno_collector.collector_helper:main"
inno-reader-helper = "inno_collector.reader_helper:main"
```

`packaging/collector_helper_entry.py` imports and exits through `inno_collector.collector_helper.main`; `reader_helper_entry.py` does the same for `reader_helper.main`. `packaging/moore_exporter_entry.py` imports and exits through `wechat_exporter.main` from the adjacent Moore repository supplied as a PyInstaller search path. None of the entries contains other logic.

- [ ] **Step 2: Write failing build-script tests**

Mock `subprocess.run` and assert three independent PyInstaller commands, distinct work/spec/output directories, `--onefile`, exact names `InnoCollectorHelper`, `InnoReaderHelper`, and `MooreExporterHelper`, and no use of `--collect-all inno_collector`. The Moore build must add only `wechat_exporter.py` and `wechat_downloader.py` from `../moore-wechat-article-downloader/scripts`. Add a binary audit test that rejects reader helper string-table hits for `wechat_exporter`, `wechat_downloader`, `MooreExporterAdapter`, `collector_helper`, `auth-key`, and `.moore`.

- [ ] **Step 3: Run and confirm the script is missing**

Run: `./.venv/bin/python -m unittest tests.test_build_helpers -v`

Expected: FAIL.

- [ ] **Step 4: Implement deterministic helper builds**

`scripts/build_helpers.py` accepts `--output <dir>`, `--moore-source <dir>` defaulting to `../moore-wechat-article-downloader/scripts`, and `--clean`. It invokes `python -m PyInstaller` once per entry with `--noconfirm`, `--clean`, `--onefile`, a role-specific name, and role-specific `--distpath`, `--workpath`, and `--specpath`. After building, run collector and reader binaries with a `status` request, run `MooreExporterHelper --help`, then run `strings` on the reader binary and reject the collector-only markers.

Return exit 0 only when all three binaries pass smoke and role audits. Never print helper stdout containing local paths; print only names, sizes, and SHA-256 values.

- [ ] **Step 5: Ignore build products and run tests**

Add `.build-macos/`, `macos/.build/`, and `*.spec` to `.gitignore`.

Run: `./.venv/bin/python -m unittest tests.test_build_helpers -v`

Expected: PASS.

- [ ] **Step 6: Build real helpers and verify role isolation**

Run: `./.venv/bin/python -m pip install -e '.[build]'`

Run: `./.venv/bin/python scripts/build_helpers.py --output .build-macos/helpers --clean`

Expected: three executable files, two status smoke tests plus the Moore help smoke PASS, and the reader audit reports zero collector-only markers.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml packaging/collector_helper_entry.py packaging/reader_helper_entry.py packaging/moore_exporter_entry.py scripts/build_helpers.py tests/test_build_helpers.py .gitignore
git commit -m "build: freeze role-specific app helpers"
```

### Task 3: Assemble valid ad-hoc signed app bundles

**Files:**
- Create: `packaging/Info-Collector.plist`
- Create: `packaging/Info-Reader.plist`
- Create: `packaging/collector.entitlements`
- Create: `packaging/reader.entitlements`
- Create: `scripts/build_macos_apps.py`
- Create: `tests/test_build_macos_apps.py`

- [ ] **Step 1: Define bundle metadata**

Use bundle identifiers `com.inno.news.collector` and `com.inno.news.reader`, minimum macOS 13.0, display names `英诺资讯采集` and `英诺资讯阅读`, version `0.1.0`, and build `1`. Declare ZIP/update-package import only in the reader plist and draft-package import only in the collector plist. Set `LSApplicationCategoryType` to `public.app-category.productivity`.

The reader entitlements contain only hardened runtime defaults. The collector entitlements add outgoing network client access because its helper reaches public article services. Neither entitlement grants incoming network server, camera, microphone, contacts, calendars, or broad user-selected file access beyond standard open/save panels.

- [ ] **Step 2: Write failing bundle-layout tests**

Build temporary fake Swift/helper executables and assert this exact layout:

```text
InnoCollector.app/Contents/MacOS/InnoCollectorApp
InnoCollector.app/Contents/PlugIns/InnoCollectorHelper
InnoCollector.app/Contents/PlugIns/MooreExporterHelper
InnoCollector.app/Contents/Resources/config/projects.json
InnoCollector.app/Contents/Resources/ThirdPartyLicenses/*
InnoCollector.app/Contents/Info.plist

InnoReader.app/Contents/MacOS/InnoReaderApp
InnoReader.app/Contents/PlugIns/InnoReaderHelper
InnoReader.app/Contents/Resources/ThirdPartyLicenses/*
InnoReader.app/Contents/Info.plist
```

Assert the collector resource is byte-identical to the existing `config/projects.json`; do not regenerate or normalize it. Assert the reader bundle has no collector helper, Moore exporter helper, project config, exporter script, `.moore` path, Cookie, Token, or auth-key markers.

- [ ] **Step 3: Run and confirm missing bundler**

Run: `./.venv/bin/python -m unittest tests.test_build_macos_apps -v`

Expected: FAIL.

- [ ] **Step 4: Implement the bundle assembler**

`scripts/build_macos_apps.py` accepts `--configuration debug|release`, `--output`, and `--skip-build`. Without `--skip-build`, run `swift build --package-path macos --configuration <value>` and the helper builder. Create app bundles in a temporary sibling directory, copy files without following symlinks, copy the existing `config/projects.json` only into collector Resources, set executables to mode `0755`, validate plists with `plutil -lint`, ad-hoc sign each helper and app executable, then sign the outer app with `codesign --force --options runtime --sign -`. Verify with `codesign --verify --deep --strict` before moving bundles into the output directory without overwriting existing bundles.

- [ ] **Step 5: Run tests and build real apps**

Run: `./.venv/bin/python -m unittest tests.test_build_macos_apps -v`

Expected: PASS.

Run: `./.venv/bin/python scripts/build_macos_apps.py --configuration release --output .build-macos/apps`

Expected: both `.app` bundles pass `plutil` and `codesign --verify`; reader audit reports zero forbidden artifacts.

- [ ] **Step 6: Commit**

```bash
git add packaging/Info-Collector.plist packaging/Info-Reader.plist packaging/collector.entitlements packaging/reader.entitlements scripts/build_macos_apps.py tests/test_build_macos_apps.py
git commit -m "build: assemble role-isolated macOS apps"
```

### Task 4: Add Developer ID signing, DMG, and notarization

**Files:**
- Create: `scripts/release_macos.py`
- Create: `tests/test_release_macos.py`

- [ ] **Step 1: Write failing release validation tests**

Assert that release mode refuses an empty `MACOS_SIGNING_IDENTITY`, refuses missing `APPLE_ID`, `APPLE_TEAM_ID`, or `APPLE_APP_PASSWORD` when notarization is requested, signs nested helpers before outer apps, creates two separate DMGs, submits each with `--wait`, staples the accepted ticket, and writes a release manifest with SHA-256 hashes.

- [ ] **Step 2: Run and confirm missing release tool**

Run: `./.venv/bin/python -m unittest tests.test_release_macos -v`

Expected: FAIL.

- [ ] **Step 3: Implement fail-closed release commands**

`scripts/release_macos.py` accepts `--apps`, `--output`, `--version`, and `--notarize`. It must:

1. require a `Developer ID Application:` identity from `MACOS_SIGNING_IDENTITY`;
2. copy each ad-hoc app to a staging directory;
3. sign each role helper/sidecar with the role entitlement, sign the Swift executable, then sign the outer app with hardened runtime and timestamp;
4. verify with `codesign --verify --deep --strict --verbose=2` and `spctl --assess --type execute`;
5. create one compressed DMG per app with `hdiutil create`;
6. when `--notarize` is set, submit using the three Apple environment variables, require status `Accepted`, staple, and re-run `spctl`;
7. write `release-manifest.json` containing version, build, app bundle IDs, DMG names, sizes, SHA-256 hashes, and notarization status.

Never include Apple credentials in command logs, manifest output, exceptions, or subprocess error messages.

- [ ] **Step 4: Run tests and an unsigned dry run**

Run: `./.venv/bin/python -m unittest tests.test_release_macos -v`

Expected: PASS.

Run without credentials and assert failure is explicit and sanitized:

```bash
env -u MACOS_SIGNING_IDENTITY ./.venv/bin/python scripts/release_macos.py --apps .build-macos/apps --output .build-macos/release --version 0.1.0
```

Expected: exit 2 with `MACOS_SIGNING_IDENTITY is required`, with no app mutation.

- [ ] **Step 5: Commit**

```bash
git add scripts/release_macos.py tests/test_release_macos.py
git commit -m "build: sign and notarize macOS releases"
```

### Task 5: Verify the complete two-user workflow

**Files:**
- Modify: `tests/test_distribution_end_to_end.py`
- Modify: `README.md`
- Create: `docs/macos-release-checklist.md`

- [ ] **Step 1: Add an automated role-separated end-to-end test**

Use two temporary HOME/Application Support roots. Run the frozen collector helper to build a baseline from the existing offline exporter fixture, run the reader helper to import it, create a draft, build and apply a second update, assert the draft bytes are unchanged, export the draft, receive it with the collector helper, and scan both simulated distributions for secrets and forbidden role artifacts.

- [ ] **Step 2: Run the automated distribution test**

Run with real helper paths:

```bash
INNO_COLLECTOR_HELPER=.build-macos/helpers/collector/InnoCollectorHelper \
INNO_READER_HELPER=.build-macos/helpers/reader/InnoReaderHelper \
./.venv/bin/python -m unittest tests.test_distribution_end_to_end -v
```

Expected: PASS without network access.

- [ ] **Step 3: Document installation and the exact manual release check**

`README.md` must distinguish the two apps, explain that only the collector Mac holds login state, show baseline/update/draft package flows, recommend Obsidian, and state article copyright and MIT attribution. `docs/macos-release-checklist.md` must require:

1. a fresh non-developer macOS 13+ user account;
2. Gatekeeper-open verification for each notarized DMG;
3. collector preflight and one controlled collection;
4. baseline import on a second account with Python and Codex absent;
5. networking disabled while searching, reading, and opening the HTML dashboard;
6. creation of a draft, second update import, and draft byte preservation;
7. draft package return and collector receipt;
8. `codesign`, `spctl`, package hash, secret scan, and third-party license checks.

- [ ] **Step 4: Run all automated gates**

Run: `./.venv/bin/python -m unittest discover -s tests -q`

Expected: all Python tests PASS.

Run: `cd macos && swift test`

Expected: all Swift tests PASS.

Run: `./.venv/bin/python scripts/build_macos_apps.py --configuration release --output .build-macos/apps`

Expected: two verified app bundles with role audits PASS.

- [ ] **Step 5: Perform and record the manual clean-account check**

Complete every checkbox in `docs/macos-release-checklist.md`, record the app version, macOS version, DMG hashes, and tester initials, and attach no screenshots containing local usernames or credentials.

- [ ] **Step 6: Commit**

```bash
git add tests/test_distribution_end_to_end.py README.md docs/macos-release-checklist.md
git commit -m "test: verify the complete macOS distribution workflow"
```

## Phase-three acceptance gate

Accept the product for friend distribution only when both role-isolated app bundles pass automated tests, the reader bundle contains no collector capability or credential paths, both required MIT notices are present, Gatekeeper accepts notarized DMGs, and the clean-account baseline/update/draft workflow is recorded as passing.
