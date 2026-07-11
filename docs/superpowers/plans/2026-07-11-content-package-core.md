# Content Package Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the verified Python core with editable Vault zones, deterministic offline dashboards, versioned baseline/incremental update packages, draft return packages, atomic imports, and role-specific JSON helpers.

**Architecture:** Keep the Vault as the source of truth and preserve the existing collector pipeline. New package modules operate only on explicit path zones and reuse the current inventory, secret scanning, hashing, and atomic-write rules. Collector and reader helpers expose separate command whitelists over one-request/one-response JSON so the future macOS apps never parse terminal text.

**Tech Stack:** Python 3.11, standard library, `unittest`, ZIP/JSON/HTML, existing `inno_collector` modules.

---

**Design reference:** `docs/superpowers/specs/2026-07-11-macos-local-news-suite-design.md`

## File map

- Modify `src/inno_collector/package.py`: expand the safe Vault path policy and expose reusable safe inventory helpers.
- Modify `src/inno_collector/vault.py`: create editable and dashboard directories without writing user content.
- Create `src/inno_collector/content_manifest.py`: deterministic file inventory and update manifest validation.
- Create `src/inno_collector/update_package.py`: build baseline/incremental packages and apply them atomically.
- Create `src/inno_collector/draft_package.py`: export, receive, and accept versioned human drafts.
- Create `src/inno_collector/dashboard.py`: generate one self-contained offline HTML dashboard.
- Create `src/inno_collector/helper_protocol.py`: strict JSON request/response framing and diagnostic sanitization.
- Modify `src/inno_collector/exporter.py`: allow a frozen exporter executable as the command prefix while preserving the current script mode.
- Create `src/inno_collector/collector_helper.py`: collector-only command dispatch.
- Create `src/inno_collector/reader_helper.py`: reader-only command dispatch.
- Modify `src/inno_collector/cli.py`: add developer-facing commands that call the same core APIs.
- Create `tests/test_content_manifest.py`, `tests/test_update_package.py`, `tests/test_draft_package.py`, `tests/test_dashboard.py`, and `tests/test_helper_protocol.py`.
- Modify `tests/test_exporter.py` for frozen exporter command coverage.
- Modify `tests/test_package.py`, `tests/test_vault.py`, and `tests/test_end_to_end.py` for compatibility and complete round-trip coverage.

### Task 1: Establish immutable and editable Vault zones

**Files:**
- Modify: `src/inno_collector/package.py`
- Modify: `src/inno_collector/vault.py`
- Test: `tests/test_package.py`
- Test: `tests/test_vault.py`

- [ ] **Step 1: Write failing path-policy tests**

Add tests that require Markdown and safe images under `10-编辑稿/`, Markdown under `11-个人笔记/`, and exactly `index.html` under `80-离线看板/`, while continuing to reject executables, databases, symlinks, nested ZIPs, and files outside the whitelist.

```python
def test_editable_and_dashboard_zones_have_narrow_whitelists(self) -> None:
    allowed = {
        "10-编辑稿/稿件.md": "---\ndraft_id: draft-1\n---\n\n正文",
        "10-编辑稿/附件/draft-1/image.png": b"png",
        "11-个人笔记/笔记.md": "个人笔记",
        "80-离线看板/index.html": "<!doctype html><title>看板</title>",
    }
    for relative, payload in allowed.items():
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
    self.assertEqual(lint_vault(self.vault)["forbidden_files"], [])

    for relative in (
        "10-编辑稿/run.command",
        "11-个人笔记/state.db",
        "80-离线看板/app.js",
        "80-离线看板/nested/index.html",
    ):
        with self.subTest(relative=relative):
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
            self.assertIn(relative, lint_vault(self.vault)["forbidden_files"])
            path.unlink()
```

- [ ] **Step 2: Run the focused tests and confirm the new allowed paths fail**

Run: `./.venv/bin/python -m unittest tests.test_package.PackageTests.test_editable_and_dashboard_zones_have_narrow_whitelists -v`

Expected: FAIL because the current whitelist rejects all three new top-level zones.

- [ ] **Step 3: Add explicit zone constants and policy branches**

Add these constants and extend `_allowed_delivery_path` without broad wildcard allowances:

```python
_SOURCE_ROOTS = {"02-项目", "03-文章", "04-附件", "90-系统"}
_HUMAN_ROOTS = {"10-编辑稿", "11-个人笔记"}
_DASHBOARD_ROOT = "80-离线看板"


def _allowed_human_path(path: PurePosixPath) -> bool:
    if path.parts[0] == "11-个人笔记":
        return path.suffix.casefold() == ".md"
    if path.parts[0] != "10-编辑稿":
        return False
    if path.suffix.casefold() == ".md":
        return True
    return (
        len(path.parts) >= 4
        and path.parts[1] == "附件"
        and path.suffix.casefold() in _IMAGE_EXTENSIONS
    )
```

In `_allowed_delivery_path`, accept directories beneath the two human roots, accept human files only through `_allowed_human_path`, and accept only `80-离线看板/index.html` in the dashboard zone.

- [ ] **Step 4: Create empty zones during Vault initialization**

In `VaultWriter._apply_locked`, include the three new directories in the existing safe directory creation sequence:

```python
for relative in (
    "02-项目",
    "03-文章",
    "04-附件",
    "10-编辑稿",
    "11-个人笔记",
    "80-离线看板",
    "90-系统",
):
    (self.root / relative).mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 5: Run Vault and package tests**

Run: `./.venv/bin/python -m unittest tests.test_package tests.test_vault -q`

Expected: PASS with no regression in the original delivery whitelist tests.

- [ ] **Step 6: Commit**

```bash
git add src/inno_collector/package.py src/inno_collector/vault.py tests/test_package.py tests/test_vault.py
git commit -m "feat: add protected Vault workspace zones"
```

### Task 2: Define deterministic content manifests

**Files:**
- Create: `src/inno_collector/content_manifest.py`
- Create: `tests/test_content_manifest.py`

- [ ] **Step 1: Write failing canonical-manifest tests**

```python
class ContentManifestTests(unittest.TestCase):
    def test_inventory_is_deterministic_and_excludes_human_content(self) -> None:
        first = build_content_manifest(self.vault, created_at="2026-07-11T12:00:00Z")
        (self.vault / "10-编辑稿/private.md").write_text("人工稿", encoding="utf-8")
        second = build_content_manifest(self.vault, created_at="2026-07-11T12:00:00Z")
        self.assertEqual(first.content_version, second.content_version)
        self.assertFalse(any(row.path.startswith("10-编辑稿/") for row in second.files))

    def test_content_version_changes_when_source_bytes_change(self) -> None:
        first = build_content_manifest(self.vault, created_at="2026-07-11T12:00:00Z")
        page = next((self.vault / "03-文章").rglob("*.md"))
        page.write_text(page.read_text(encoding="utf-8") + "\n变化", encoding="utf-8")
        second = build_content_manifest(self.vault, created_at="2026-07-11T12:00:00Z")
        self.assertNotEqual(first.content_version, second.content_version)
```

- [ ] **Step 2: Run and confirm import failure**

Run: `./.venv/bin/python -m unittest tests.test_content_manifest -v`

Expected: FAIL with `ModuleNotFoundError: inno_collector.content_manifest`.

- [ ] **Step 3: Add frozen manifest types and canonical hashing**

```python
@dataclass(frozen=True, slots=True)
class ContentFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ContentManifest:
    format_version: int
    created_at: str
    content_version: str
    files: tuple[ContentFile, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "created_at": self.created_at,
            "content_version": self.content_version,
            "files": [asdict(row) for row in self.files],
        }
```

`build_content_manifest` must inventory regular files only in `00-首页.md`, `01-采集状态.md`, `02-项目`, `03-文章`, `04-附件`, `80-离线看板`, and allowed `90-系统` files; sort by NFC-normalized POSIX path; hash each file with SHA-256; and derive `content_version` from compact UTF-8 JSON of the file rows only, so timestamps do not change the version.

- [ ] **Step 4: Add strict parsing**

Implement `parse_content_manifest(payload: object) -> ContentManifest` that rejects booleans as integers, unknown format versions, duplicate paths, non-lowercase 64-character hashes, unsafe paths, unsorted rows, and a `content_version` that does not match the canonical file rows.

- [ ] **Step 5: Run manifest tests and the full baseline**

Run: `./.venv/bin/python -m unittest tests.test_content_manifest -v`

Expected: PASS.

Run: `./.venv/bin/python -m unittest discover -s tests -q`

Expected: at least 241 tests, all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/inno_collector/content_manifest.py tests/test_content_manifest.py
git commit -m "feat: define deterministic content manifests"
```

### Task 3: Build baseline and incremental update packages

**Files:**
- Create: `src/inno_collector/update_package.py`
- Create: `tests/test_update_package.py`

- [ ] **Step 1: Write failing package-diff tests**

Create a test Vault, build a baseline, mutate one source article, add one source file, remove one project page, and build an incremental package against the baseline manifest. Assert that `added`, `changed`, and `deleted` contain exactly those paths and never contain human-zone paths.

```python
result = build_update_package(
    vault,
    output,
    base_package=baseline,
    created_at="2026-07-11T12:30:00Z",
)
self.assertEqual(result["base_version"], baseline_manifest.content_version)
self.assertEqual(result["target_version"], target_manifest.content_version)
self.assertEqual(result["deleted"], ["02-项目/已删除.md"])
self.assertNotIn("10-编辑稿/我的稿件.md", result["included"])
```

- [ ] **Step 2: Run and confirm the missing API failure**

Run: `./.venv/bin/python -m unittest tests.test_update_package.UpdatePackageBuildTests -v`

Expected: FAIL because `build_update_package` does not exist.

- [ ] **Step 3: Add update manifest types**

Use this archive layout:

```text
update-manifest.json
payload/<safe relative Vault path>
```

The manifest fields are exactly `format_version`, `kind`, `created_at`, `base_version`, `target_version`, `files`, and `deleted`. `kind` is `baseline` when `base_version` is `null`, otherwise `incremental`. Each file row carries `path`, `size`, and `sha256`.

- [ ] **Step 4: Implement package construction through a temporary file**

`build_update_package(vault, output, base_package=None, created_at=None)` must:

1. run `lint_vault` and reject any error;
2. build the target manifest;
3. parse the previous package's update manifest when supplied;
4. compute added/changed/deleted paths from manifest maps;
5. write only added/changed regular files plus `update-manifest.json` to a same-directory temporary ZIP;
6. reopen the temporary ZIP and revalidate every member;
7. claim the requested output without overwriting an existing file.

Return a JSON-serializable dictionary with `package_path`, `kind`, `base_version`, `target_version`, `included`, `deleted`, and `package_sha256`.

- [ ] **Step 5: Run security and determinism tests**

Cover existing-output refusal, base package corruption, unsafe ZIP members, symlinks, duplicated members, hash mismatch, human-zone exclusion, deterministic file ordering, and cleanup after a write failure.

Run: `./.venv/bin/python -m unittest tests.test_update_package -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/inno_collector/update_package.py tests/test_update_package.py
git commit -m "feat: build versioned update packages"
```

### Task 4: Apply update packages atomically

**Files:**
- Modify: `src/inno_collector/update_package.py`
- Modify: `tests/test_update_package.py`

- [ ] **Step 1: Write failing import tests**

The primary test must start with a reader Vault containing `10-编辑稿/保留.md` and `11-个人笔记/保留.md`, apply a valid incremental package, and assert both files are byte-identical while source files reach the target manifest. Assert delivered source, dashboard, and system files are mode `0444` while human files remain owner-writable. Add failure cases for base-version mismatch and a simulated replacement error that leaves the original Vault unchanged.

- [ ] **Step 2: Run and confirm `apply_update_package` is missing**

Run: `./.venv/bin/python -m unittest tests.test_update_package.UpdatePackageImportTests -v`

Expected: FAIL with an import or attribute error for `apply_update_package`.

- [ ] **Step 3: Implement staged import**

Add:

```python
@dataclass(frozen=True, slots=True)
class UpdateApplyResult:
    previous_version: str | None
    target_version: str
    added_or_changed: int
    deleted: int
```

`apply_update_package(package_path, vault)` must lock a sibling `.update.lock`, validate the archive before extraction, and handle two explicit starting states: a baseline package may initialize a missing Vault, while an incremental package requires an existing Vault whose content version equals `base_version`. Create the human/dashboard directories in a new baseline Vault. For an existing Vault, copy it to a fresh sibling staging directory without following symlinks and temporarily make staged source files owner-writable. Apply only declared source/dashboard/system changes, assert human-zone snapshots are unchanged, run `lint_vault`, verify the target version, set regular files in source/dashboard/system zones to `0444`, leave human files owner-writable, then swap staging and live Vault with a rollback backup. Remove staging and backup after success; restore the backup after any failed swap.

- [ ] **Step 4: Run import tests and full package tests**

Run: `./.venv/bin/python -m unittest tests.test_update_package tests.test_package -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/inno_collector/update_package.py tests/test_update_package.py
git commit -m "feat: apply updates without touching human work"
```

### Task 5: Add draft return packages and conflict-safe intake

**Files:**
- Create: `src/inno_collector/draft_package.py`
- Create: `tests/test_draft_package.py`

- [ ] **Step 1: Write failing round-trip and conflict tests**

Use a draft with strict JSON-compatible frontmatter fields `draft_id`, `draft_version`, `author`, `title`, `updated_at`, and `source_ids`. Export it, receive it into an empty inbox, accept it into a collector Vault, then receive a different payload with the same ID/version and assert both versions remain available for manual choice.

- [ ] **Step 2: Run and verify the module is absent**

Run: `./.venv/bin/python -m unittest tests.test_draft_package -v`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Add strict draft metadata parsing**

```python
@dataclass(frozen=True, slots=True)
class DraftMetadata:
    draft_id: str
    draft_version: int
    author: str
    title: str
    updated_at: str
    source_ids: tuple[str, ...]
```

Accept `draft_id` values matching `[a-z0-9][a-z0-9-]{7,63}`, positive integer versions excluding booleans, nonblank bounded text fields, ISO-8601 timestamps, and unique `sha256:` source IDs. Reject secrets and absolute local paths in draft text and attachments.

- [ ] **Step 4: Implement export, receive, and accept APIs**

- `build_draft_package(vault, draft_paths, output, exported_at)` writes `draft-manifest.json` and only the selected Markdown plus referenced safe attachments.
- `receive_draft_package(package_path, inbox)` validates and extracts to `inbox/<package_sha256>/` without overwriting an existing receipt.
- `accept_received_draft(receipt, vault)` writes a new draft directly when the ID is absent, treats identical ID/version/content as idempotent, and writes a sibling filename suffixed with the first 12 hash characters when content conflicts.

All three APIs return JSON-serializable dictionaries.

- [ ] **Step 5: Run draft tests**

Run: `./.venv/bin/python -m unittest tests.test_draft_package -v`

Expected: PASS, including traversal, duplicate member, secret, collision, idempotency, and simulated write-failure cases.

- [ ] **Step 6: Commit**

```bash
git add src/inno_collector/draft_package.py tests/test_draft_package.py
git commit -m "feat: exchange human drafts safely"
```

### Task 6: Generate a self-contained offline dashboard

**Files:**
- Create: `src/inno_collector/dashboard.py`
- Create: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing dashboard tests**

Assert that generated HTML contains escaped project/article data, success/partial counts, local search controls, and no external stylesheet/script/font references. Generate twice from identical Vault content and require byte-identical output.

- [ ] **Step 2: Run and confirm the missing module**

Run: `./.venv/bin/python -m unittest tests.test_dashboard -v`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement deterministic one-file rendering**

Add `build_dashboard(vault: Path) -> Path`. Read only `90-系统/manifest.json` and the collection report, serialize the required public fields with `json.dumps(..., ensure_ascii=False, sort_keys=True)`, escape the JSON for embedding in a script element, and render all CSS and JavaScript inline. The JavaScript may filter already-embedded rows but must not call `fetch`, create network requests, or mutate the Vault.

- [ ] **Step 4: Integrate dashboard generation before package manifest creation**

Call `build_dashboard(vault)` from `build_update_package` before linting and manifest inventory. If rendering fails, propagate a sanitized package error and leave any prior dashboard untouched by writing to a sibling temporary file before `os.replace`.

- [ ] **Step 5: Run dashboard, update-package, and full tests**

Run: `./.venv/bin/python -m unittest tests.test_dashboard tests.test_update_package -v`

Expected: PASS.

Run: `./.venv/bin/python -m unittest discover -s tests -q`

Expected: all tests PASS with a count greater than 239.

- [ ] **Step 6: Commit**

```bash
git add src/inno_collector/dashboard.py src/inno_collector/update_package.py tests/test_dashboard.py tests/test_update_package.py
git commit -m "feat: add offline results dashboard"
```

### Task 7: Add role-specific JSON helpers and end-to-end coverage

**Files:**
- Create: `src/inno_collector/helper_protocol.py`
- Create: `src/inno_collector/collector_helper.py`
- Create: `src/inno_collector/reader_helper.py`
- Modify: `src/inno_collector/cli.py`
- Create: `tests/test_helper_protocol.py`
- Modify: `tests/test_end_to_end.py`

- [ ] **Step 1: Write failing protocol tests**

Collector commands are exactly `status`, `collect`, `build_update`, `receive_drafts`, and `accept_draft`. Reader commands are exactly `status`, `preview_update`, `apply_update`, `create_draft`, `build_drafts`, and `rebuild_dashboard`. Assert that the reader helper rejects `collect` before importing collector-only modules and that all failures return one sanitized JSON object on stdout with a nonzero exit code.

- [ ] **Step 2: Run and confirm missing helpers**

Run: `./.venv/bin/python -m unittest tests.test_helper_protocol -v`

Expected: FAIL because the helper modules do not exist.

- [ ] **Step 3: Implement one-request/one-response framing**

```python
def run_helper(
    handlers: dict[str, Callable[[dict[str, object]], dict[str, object]]],
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    try:
        request = json.loads(input_stream.read())
        if not isinstance(request, dict) or set(request) != {"id", "command", "arguments"}:
            raise ValueError("invalid helper request")
        request_id = request["id"]
        command = request["command"]
        arguments = request["arguments"]
        if not isinstance(request_id, str) or not isinstance(command, str) or not isinstance(arguments, dict):
            raise ValueError("invalid helper request")
        handler = handlers.get(command)
        if handler is None:
            raise ValueError("unsupported helper command")
        response = {"id": request_id, "ok": True, "result": handler(arguments)}
        exit_code = 0
    except Exception as exc:
        response = {"id": request_id if "request_id" in locals() else "", "ok": False, "error": sanitize_diagnostic(exc)}
        exit_code = 2
    output_stream.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code
```

- [ ] **Step 4: Add a frozen-exporter command boundary**

Extend `MooreExporterAdapter` with `command_prefix: tuple[str, ...] | None = None`. Default to `(sys.executable, str(script))` for current development behavior; when supplied, `_execute` must begin argv with that exact prefix and must not append the script path. Add `tests/test_exporter.py` cases for a single frozen executable path, an empty-prefix rejection, and unchanged legacy argv.

- [ ] **Step 5: Implement separate dispatch modules**

Keep imports inside each handler module so `reader_helper.py` never imports `pipeline`, `exporter`, or collector configuration. Add `main()` entry points that call `run_helper` with the exact role whitelist. In a frozen collector helper, resolve a sibling `MooreExporterHelper` from `Path(sys.executable).parent`; in source mode, keep the existing script/runtime defaults. Resolve `projects.json` only from an explicit request argument or the collector app's Resources path, never by copying or rewriting its contents.

- [ ] **Step 6: Add CLI wrappers and a complete offline round trip**

Add CLI subcommands `package-update`, `apply-update`, `package-drafts`, `receive-drafts`, and `dashboard` as developer tools that invoke the same functions. Extend `tests/test_end_to_end.py` to run:

1. fake offline collection;
2. baseline package creation;
3. baseline import into a reader Vault;
4. reader draft creation;
5. second collection and incremental import;
6. byte-for-byte preservation of the draft;
7. draft package export and collector receipt.

- [ ] **Step 7: Run focused and full verification**

Run: `./.venv/bin/python -m unittest tests.test_helper_protocol tests.test_end_to_end -v`

Expected: PASS.

Run: `./.venv/bin/python -m unittest discover -s tests -q`

Expected: all tests PASS; no test performs a network request.

- [ ] **Step 8: Commit**

```bash
git add src/inno_collector/exporter.py src/inno_collector/helper_protocol.py src/inno_collector/collector_helper.py src/inno_collector/reader_helper.py src/inno_collector/cli.py tests/test_exporter.py tests/test_helper_protocol.py tests/test_end_to_end.py
git commit -m "feat: expose collector and reader helper protocols"
```

## Phase-one acceptance gate

Run all commands from the repository root:

```bash
./.venv/bin/python -m unittest discover -s tests -q
./.venv/bin/python -m inno_collector.collector_helper <<<'{"id":"smoke","command":"status","arguments":{}}'
./.venv/bin/python -m inno_collector.reader_helper <<<'{"id":"smoke","command":"status","arguments":{}}'
```

Accept this phase only when the complete suite passes, both helpers return exactly one JSON response, the reader helper rejects collector commands, an incremental import preserves human-zone bytes, and the offline dashboard loads with networking disabled.
