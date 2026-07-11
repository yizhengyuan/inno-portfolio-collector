from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from inno_collector.content_manifest import (
    ContentManifestError,
    build_content_manifest,
    parse_content_manifest,
)
from inno_collector.identity import article_key
from inno_collector.models import NormalizedArticle, ProjectRunResult
from inno_collector.vault import VaultWriter


class ContentManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.vault = self.root / "vault"
        body = "# 正文\n\n用于内容清单测试。\n"
        source = self.root / "source.md"
        source.write_text(body, encoding="utf-8")
        url = "https://mp.weixin.qq.com/s/content-manifest"
        article = NormalizedArticle(
            key=article_key(url),
            project="项目甲",
            account="账号甲",
            title="内容清单文章",
            published="2026-07-11",
            source_url=url,
            collected_at="2026-07-11T12:00:00+08:00",
            content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            body=body,
            source_markdown=source,
        )
        result = ProjectRunResult(
            project="项目甲",
            account="账号甲",
            discovered=1,
            downloaded=1,
            skipped=0,
            failed=0,
            status="success",
            error="",
        )
        VaultWriter(self.vault).apply([article], [result])

    def test_inventory_is_deterministic_and_excludes_human_content(self) -> None:
        first = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        )
        draft = self.vault / "10-编辑稿/private.md"
        draft.write_text("人工稿", encoding="utf-8")
        second = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:30:00Z",
        )

        self.assertEqual(first.content_version, second.content_version)
        self.assertNotEqual(first.created_at, second.created_at)
        self.assertFalse(
            any(row.path.startswith("10-编辑稿/") for row in second.files)
        )
        self.assertEqual(
            tuple(row.path for row in second.files),
            tuple(sorted(row.path for row in second.files)),
        )

    def test_content_version_changes_when_source_bytes_change(self) -> None:
        first = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        )
        page = next((self.vault / "03-文章").rglob("*.md"))
        page.write_text(
            page.read_text(encoding="utf-8") + "\n来源变化",
            encoding="utf-8",
        )

        second = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        )

        self.assertNotEqual(first.content_version, second.content_version)

    def test_round_trip_uses_only_json_scalars(self) -> None:
        manifest = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        )
        payload = json.loads(
            json.dumps(manifest.as_dict(), ensure_ascii=False, sort_keys=True)
        )

        self.assertEqual(parse_content_manifest(payload), manifest)

    def test_parser_rejects_unsorted_duplicate_and_unsafe_rows(self) -> None:
        manifest = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        ).as_dict()
        rows = list(manifest["files"])
        mutations = (
            list(reversed(rows)),
            rows + [dict(rows[0])],
            [{**rows[0], "path": "../escape"}] + rows[1:],
            [{**rows[0], "path": "10-编辑稿/private.md"}] + rows[1:],
        )
        for candidate_rows in mutations:
            with self.subTest(rows=candidate_rows[:1]):
                candidate = dict(manifest)
                candidate["files"] = candidate_rows
                with self.assertRaises(ContentManifestError):
                    parse_content_manifest(candidate)

    def test_parser_rejects_boolean_size_unknown_fields_and_wrong_version(self) -> None:
        manifest = build_content_manifest(
            self.vault,
            created_at="2026-07-11T12:00:00Z",
        ).as_dict()
        first_row = dict(manifest["files"][0])
        candidates = []

        boolean_size = dict(manifest)
        boolean_size["files"] = [{**first_row, "size": True}] + list(
            manifest["files"][1:]
        )
        candidates.append(boolean_size)

        unknown_field = dict(manifest)
        unknown_field["unexpected"] = "x"
        candidates.append(unknown_field)

        wrong_version = dict(manifest)
        wrong_version["content_version"] = "sha256:" + "0" * 64
        candidates.append(wrong_version)

        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ContentManifestError):
                    parse_content_manifest(candidate)


if __name__ == "__main__":
    unittest.main()
