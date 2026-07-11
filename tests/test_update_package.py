from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from inno_collector.content_manifest import build_content_manifest
from inno_collector.identity import article_key
from inno_collector.models import NormalizedArticle, ProjectRunResult
from inno_collector.update_package import UpdatePackageError, build_update_package
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


if __name__ == "__main__":
    unittest.main()
