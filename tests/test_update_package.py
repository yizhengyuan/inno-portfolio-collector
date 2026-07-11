from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from inno_collector.content_manifest import build_content_manifest
from inno_collector.identity import article_key
from inno_collector.models import NormalizedArticle, ProjectRunResult
from inno_collector.update_package import (
    UpdatePackageError,
    apply_update_package,
    build_update_package,
)
from inno_collector.vault import VaultWriter


class UpdatePackageBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.vault = self.root / "vault"
        self.url = "https://mp.weixin.qq.com/s/update-package"
        self.result = ProjectRunResult(
            project="项目甲",
            account="账号甲",
            discovered=1,
            downloaded=1,
            skipped=0,
            failed=0,
            status="success",
            error="",
        )
        self.write_article("首版正文")

    def write_article(self, text: str) -> None:
        body = f"# 正文\n\n{text}\n"
        source = self.root / "source.md"
        source.write_text(body, encoding="utf-8")
        article = NormalizedArticle(
            key=article_key(self.url),
            project="项目甲",
            account="账号甲",
            title="更新包文章",
            published="2026-07-11",
            source_url=self.url,
            collected_at="2026-07-11T12:00:00+08:00",
            content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            body=body,
            source_markdown=source,
        )
        VaultWriter(self.vault).apply([article], [self.result])

    @staticmethod
    def archive_manifest(package: Path) -> dict[str, object]:
        with zipfile.ZipFile(package) as archive:
            return json.loads(archive.read("update-manifest.json"))

    def test_baseline_contains_every_content_file_and_excludes_human_files(self) -> None:
        draft = self.vault / "10-编辑稿/private.md"
        draft.write_text("人工稿", encoding="utf-8")
        output = self.root / "baseline.inno-update"

        result = build_update_package(
            self.vault,
            output,
            created_at="2026-07-11T12:00:00Z",
        )
        target = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        )

        self.assertEqual(result["kind"], "baseline")
        self.assertIsNone(result["base_version"])
        self.assertEqual(result["target_version"], target.content_version)
        self.assertNotIn("10-编辑稿/private.md", result["included"])
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
        self.assertEqual(names[0], "update-manifest.json")
        self.assertEqual(
            names[1:],
            [f"payload/{row.path}" for row in target.files],
        )

    def test_incremental_records_added_changed_deleted_and_excludes_human_files(
        self,
    ) -> None:
        removed = self.vault / "80-离线看板/index.html"
        removed.write_text("<!doctype html><title>旧看板</title>", encoding="utf-8")
        baseline = self.root / "baseline.inno-update"
        build_update_package(
            self.vault,
            baseline,
            created_at="2026-07-11T12:00:00Z",
        )

        article_path = next((self.vault / "03-文章").rglob("*.md"))
        self.write_article("第二版正文")
        added = self.vault / "04-附件/extra/new.png"
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_bytes(b"png")
        removed.unlink()
        (self.vault / "10-编辑稿/private.md").write_text("人工稿", encoding="utf-8")
        output = self.root / "incremental.inno-update"

        result = build_update_package(
            self.vault,
            output,
            base_package=baseline,
            created_at="2026-07-11T12:30:00Z",
        )

        self.assertEqual(result["kind"], "incremental")
        self.assertIn(article_path.relative_to(self.vault).as_posix(), result["included"])
        self.assertIn("04-附件/extra/new.png", result["included"])
        self.assertEqual(result["deleted"], ["80-离线看板/index.html"])
        self.assertFalse(any(path.startswith("10-编辑稿/") for path in result["included"]))
        manifest = self.archive_manifest(output)
        self.assertEqual(manifest["base_version"], result["base_version"])
        self.assertEqual(manifest["target_version"], result["target_version"])

    def test_refuses_existing_output_without_changing_it(self) -> None:
        output = self.root / "claimed.inno-update"
        output.write_bytes(b"owner data")

        with self.assertRaises(UpdatePackageError):
            build_update_package(
                self.vault,
                output,
                created_at="2026-07-11T12:00:00Z",
            )

        self.assertEqual(output.read_bytes(), b"owner data")

    def test_rejects_corrupt_base_package(self) -> None:
        base = self.root / "bad.inno-update"
        with zipfile.ZipFile(base, "w") as archive:
            archive.writestr("update-manifest.json", "{}")

        with self.assertRaises(UpdatePackageError):
            build_update_package(
                self.vault,
                self.root / "incremental.inno-update",
                base_package=base,
                created_at="2026-07-11T12:00:00Z",
            )

    def test_source_file_injected_after_manifest_is_rejected(self) -> None:
        output = self.root / "raced.inno-update"

        def inject_after_manifest(vault: Path, *, created_at: str):
            manifest = build_content_manifest(vault, created_at=created_at)
            late = self.vault / "04-附件/late/new.png"
            late.parent.mkdir(parents=True, exist_ok=True)
            late.write_bytes(b"late")
            return manifest

        with patch(
            "inno_collector.update_package.build_content_manifest",
            side_effect=inject_after_manifest,
        ):
            with self.assertRaises(UpdatePackageError):
                build_update_package(
                    self.vault,
                    output,
                    created_at="2026-07-11T12:00:00Z",
                )

        self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".inno-update-*.tmp")))


class UpdatePackageImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.collector_vault = self.root / "collector-vault"
        self.reader_vault = self.root / "reader-vault"
        self.url = "https://mp.weixin.qq.com/s/update-import"
        self.project_result = ProjectRunResult(
            project="项目甲",
            account="账号甲",
            discovered=1,
            downloaded=1,
            skipped=0,
            failed=0,
            status="success",
            error="",
        )
        self.write_article("首版正文")

    def write_article(self, text: str) -> None:
        body = f"# 正文\n\n{text}\n"
        source = self.root / "import-source.md"
        source.write_text(body, encoding="utf-8")
        article = NormalizedArticle(
            key=article_key(self.url),
            project="项目甲",
            account="账号甲",
            title="导入测试文章",
            published="2026-07-11",
            source_url=self.url,
            collected_at="2026-07-11T12:00:00+08:00",
            content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            body=body,
            source_markdown=source,
        )
        VaultWriter(self.collector_vault).apply([article], [self.project_result])

    def build_baseline(self) -> Path:
        package = self.root / "baseline.inno-update"
        build_update_package(
            self.collector_vault,
            package,
            created_at="2026-07-11T12:00:00Z",
        )
        return package

    def test_baseline_initializes_missing_reader_vault_and_protects_sources(self) -> None:
        package = self.build_baseline()

        result = apply_update_package(package, self.reader_vault)

        self.assertIsNone(result.previous_version)
        target = build_content_manifest(
            self.reader_vault,
            created_at="2026-07-11T12:00:00Z",
        )
        self.assertEqual(result.target_version, target.content_version)
        for relative in ("10-编辑稿", "11-个人笔记", "80-离线看板"):
            self.assertTrue((self.reader_vault / relative).is_dir())
        article = next((self.reader_vault / "03-文章").rglob("*.md"))
        self.assertEqual(stat.S_IMODE(article.stat().st_mode), 0o444)
        self.assertTrue(
            stat.S_IMODE((self.reader_vault / "10-编辑稿").stat().st_mode) & 0o200
        )

    def test_incremental_preserves_human_files_byte_for_byte(self) -> None:
        baseline = self.build_baseline()
        apply_update_package(baseline, self.reader_vault)
        draft = self.reader_vault / "10-编辑稿/保留.md"
        note = self.reader_vault / "11-个人笔记/保留.md"
        draft.write_bytes(b"draft bytes")
        note.write_bytes(b"note bytes")

        self.write_article("第二版正文")
        incremental = self.root / "incremental.inno-update"
        build_update_package(
            self.collector_vault,
            incremental,
            base_package=baseline,
            created_at="2026-07-11T12:30:00Z",
        )

        result = apply_update_package(incremental, self.reader_vault)

        self.assertIsNotNone(result.previous_version)
        self.assertEqual(draft.read_bytes(), b"draft bytes")
        self.assertEqual(note.read_bytes(), b"note bytes")
        article = next((self.reader_vault / "03-文章").rglob("*.md"))
        self.assertIn("第二版正文", article.read_text(encoding="utf-8"))

    def test_base_version_mismatch_rejects_without_changing_reader(self) -> None:
        baseline = self.build_baseline()
        apply_update_package(baseline, self.reader_vault)
        self.write_article("第二版正文")
        incremental = self.root / "incremental.inno-update"
        build_update_package(
            self.collector_vault,
            incremental,
            base_package=baseline,
            created_at="2026-07-11T12:30:00Z",
        )
        source = next((self.reader_vault / "03-文章").rglob("*.md"))
        source.chmod(0o644)
        source.write_text(source.read_text(encoding="utf-8") + "\n本地篡改", encoding="utf-8")
        before = {
            path.relative_to(self.reader_vault).as_posix(): path.read_bytes()
            for path in self.reader_vault.rglob("*")
            if path.is_file()
        }

        with self.assertRaises(UpdatePackageError):
            apply_update_package(incremental, self.reader_vault)

        after = {
            path.relative_to(self.reader_vault).as_posix(): path.read_bytes()
            for path in self.reader_vault.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_failed_stage_swap_restores_original_reader_vault(self) -> None:
        baseline = self.build_baseline()
        apply_update_package(baseline, self.reader_vault)
        self.write_article("第二版正文")
        incremental = self.root / "incremental.inno-update"
        build_update_package(
            self.collector_vault,
            incremental,
            base_package=baseline,
            created_at="2026-07-11T12:30:00Z",
        )
        before = {
            path.relative_to(self.reader_vault).as_posix(): path.read_bytes()
            for path in self.reader_vault.rglob("*")
            if path.is_file()
        }
        real_replace = __import__("os").replace

        def fail_stage_install(source: object, destination: object) -> None:
            if ".reader-vault.stage-" in str(source) and Path(destination) == self.reader_vault:
                raise OSError("simulated swap failure")
            real_replace(source, destination)

        with patch("inno_collector.update_package.os.replace", side_effect=fail_stage_install):
            with self.assertRaises(UpdatePackageError):
                apply_update_package(incremental, self.reader_vault)

        after = {
            path.relative_to(self.reader_vault).as_posix(): path.read_bytes()
            for path in self.reader_vault.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
