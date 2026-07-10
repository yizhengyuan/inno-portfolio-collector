from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from inno_collector.cli import main
from inno_collector.identity import article_key
from inno_collector.models import NormalizedArticle, ProjectRunResult
from inno_collector.package import (
    DeliveryValidationError,
    build_delivery_zip,
    lint_vault,
)
from inno_collector.vault import VaultWriter


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.vault = self.root / "英诺被投项目资讯库"
        url = "https://mp.weixin.qq.com/s/clean-article"
        body = "# 正文\n\n内容。\n"
        source = self.root / "source.md"
        source.write_text(body, encoding="utf-8")
        article = NormalizedArticle(
            key=article_key(url),
            project="项目甲",
            account="账号甲",
            title="文章甲",
            published="2026-07-10",
            source_url=url,
            collected_at="2026-07-11T09:30:00+08:00",
            content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            body=body,
            source_markdown=source,
        )
        result = ProjectRunResult(
            project="项目甲", account="账号甲", discovered=1, downloaded=1,
            skipped=0, failed=0, status="success", error="",
            last_sync="2026-07-11T09:30:00+08:00",
        )
        VaultWriter(self.vault).apply([article], [result])

    def manifest(self) -> dict:
        return json.loads(
            (self.vault / "90-系统/manifest.json").read_text(encoding="utf-8")
        )

    def test_clean_vault_lints_and_packages_single_top_level(self) -> None:
        report = lint_vault(self.vault)
        self.assertEqual(report["errors"], [])
        output = self.root / "dist" / "delivery.zip"

        result = build_delivery_zip(
            self.vault,
            output,
            now=lambda: datetime(2026, 7, 11, 10, 5),
        )

        self.assertEqual(result["article_count"], 1)
        self.assertEqual(result["failed_projects"], 0)
        self.assertEqual(result["zip_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        summary = output.with_suffix(".summary.md").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), summary)
        self.assertIn(result["zip_sha256"], summary)
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
        self.assertTrue(names)
        self.assertTrue(all(name.startswith(self.vault.name + "/") for name in names))
        self.assertFalse(any(name.endswith(".lock") for name in names))

    def test_default_name_uses_injected_time(self) -> None:
        output = build_delivery_zip(
            self.vault,
            self.root / "dist",
            now=lambda: datetime(2026, 7, 11, 10, 5),
        )["zip_path"]
        self.assertTrue(str(output).endswith("英诺被投项目资讯库-20260711-1005.zip"))

    def test_refuses_secret_and_absolute_paths_but_allows_redacted_and_field_names(self) -> None:
        bad = self.vault / "02-项目/bad.md"
        samples = (
            "auth-key=real-value", "pass_ticket: real", "appmsg_token=real",
            "Authorization: Bearer abc", "Cookie: sid=abc", "/Users/alice/private",
            "/Volumes/secret/a", "C:\\Users\\alice", "\\\\server\\share\\x",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                bad.write_text(sample, encoding="utf-8")
                self.assertTrue(lint_vault(self.vault)["secrets"])
        bad.write_text(
            "字段名：auth-key、pass_ticket、appmsg_token、Authorization、Cookie\n"
            "auth-key=[REDACTED]\n/path/[REDACTED]\n",
            encoding="utf-8",
        )
        self.assertEqual(lint_vault(self.vault)["secrets"], [])

    def test_reports_broken_wikilinks_and_local_images(self) -> None:
        page = self.vault / "02-项目/bad.md"
        page.write_text(
            "[[../03-文章/不存在.md#标题|别名]]\n![](../04-附件/missing.png)\n",
            encoding="utf-8",
        )
        report = lint_vault(self.vault)
        self.assertEqual(len(report["broken_links"]), 2)

    def test_wikilink_alias_heading_optional_md_and_encoded_image_resolve(self) -> None:
        image = self.vault / "04-附件/项目甲/a b.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"png")
        article_path = next((self.vault / "03-文章").rglob("*.md"))
        page = self.vault / "02-项目/links.md"
        relative = os.path.relpath(article_path, page.parent).replace(os.sep, "/")
        page.write_text(
            f"[[{relative[:-3]}#正文|文章]]\n![](../04-附件/项目甲/a%20b.png)\n",
            encoding="utf-8",
        )
        self.assertEqual(lint_vault(self.vault)["broken_links"], [])

    def test_rejects_forbidden_files_symlinks_special_files_and_suspicious_temps(self) -> None:
        bad_paths = (
            ".DS_Store", ".obsidian/workspace.json", "runtime/x", "staging/x",
            "90-系统/cookies.sqlite", "02-项目/file.tmp", "02-项目/file.backup",
            "02-项目/other.lock",
        )
        for relative in bad_paths:
            with self.subTest(relative=relative):
                path = self.vault / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
                self.assertTrue(lint_vault(self.vault)["forbidden_files"])
                path.unlink()
        target = self.vault / "02-项目/target.md"
        target.write_text("x", encoding="utf-8")
        link = self.vault / "02-项目/link.md"
        link.symlink_to(target)
        self.assertTrue(lint_vault(self.vault)["forbidden_files"])

    def test_rejects_manifest_frontmatter_key_url_path_and_orphan_inconsistency(self) -> None:
        manifest = self.manifest()
        key, record = next(iter(manifest["articles"].items()))
        path = self.vault / record["path"]
        mutations = (
            ("bad hash", lambda m: m["articles"][key].__setitem__("content_hash", "x")),
            ("wrong key", lambda m: m["articles"].__setitem__("sha256:" + "0" * 64, m["articles"].pop(key))),
            ("unsafe path", lambda m: m["articles"][key].__setitem__("path", "../x.md")),
        )
        original = json.loads(json.dumps(manifest))
        for name, mutate in mutations:
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(original))
                mutate(candidate)
                (self.vault / "90-系统/manifest.json").write_text(json.dumps(candidate), encoding="utf-8")
                self.assertTrue(lint_vault(self.vault)["manifest_errors"])
        (self.vault / "90-系统/manifest.json").write_text(json.dumps(original), encoding="utf-8")
        path.write_text(path.read_text(encoding="utf-8").replace('title: "文章甲"', 'title: "篡改"'), encoding="utf-8")
        self.assertTrue(lint_vault(self.vault)["manifest_errors"])
        path.write_text("orphan", encoding="utf-8")
        (path.parent / "orphan.md").write_text("orphan", encoding="utf-8")
        self.assertTrue(lint_vault(self.vault)["manifest_errors"])

    def test_rejects_nfc_casefold_manifest_path_collision(self) -> None:
        manifest = self.manifest()
        key, record = next(iter(manifest["articles"].items()))
        second = dict(record)
        second["key"] = "sha256:" + "1" * 64
        second["source_url"] = "https://mp.weixin.qq.com/s/second"
        second["path"] = record["path"].upper()
        manifest["articles"][second["key"]] = second
        (self.vault / "90-系统/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(lint_vault(self.vault)["manifest_errors"])

    def test_failure_counts_must_match_and_warning_is_required(self) -> None:
        report_path = self.vault / "90-系统/collection-report.md"
        report_path.write_text(report_path.read_text(encoding="utf-8").replace("失败项目数：0", "失败项目数：1"), encoding="utf-8")
        self.assertTrue(lint_vault(self.vault)["status_errors"])

    def test_valid_partial_failure_is_recorded_in_status_report_and_home(self) -> None:
        result = ProjectRunResult(
            project="项目甲", account="账号甲", discovered=2, downloaded=1,
            skipped=0, failed=1, status="partial", error="一个条目失败",
            last_sync="2026-07-11T09:30:00+08:00",
        )
        VaultWriter(self.vault).apply([], [result])
        report = lint_vault(self.vault)
        self.assertEqual(report["status_errors"], [])
        self.assertEqual(report["failed_projects"], 1)

    def test_output_inside_vault_is_refused_and_failed_write_leaves_no_half_package(self) -> None:
        with self.assertRaises(DeliveryValidationError):
            build_delivery_zip(self.vault, self.vault / "bad.zip")
        output = self.root / "out.zip"
        with patch("inno_collector.package.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                build_delivery_zip(self.vault, output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".summary.md").exists())
        self.assertFalse(any(self.root.glob(".*.tmp")))

    def test_cli_lint_and_package_emit_json_and_use_nonzero_for_invalid(self) -> None:
        self.assertEqual(main(["lint", "--vault", str(self.vault)]), 0)
        self.assertEqual(
            main(["package", "--vault", str(self.vault), "--dist", str(self.root / "dist")]),
            0,
        )
        (self.vault / "bad.md").write_text("Cookie: sid=secret", encoding="utf-8")
        self.assertEqual(main(["lint", "--vault", str(self.vault)]), 2)


if __name__ == "__main__":
    unittest.main()
