from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from inno_collector import vault as vault_module
from inno_collector.models import NormalizedArticle, ProjectRunResult, VaultApplyResult
from inno_collector.vault import VaultWriter


class VaultWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.vault = self.root / "vault"
        self.source = self.root / "export" / "article.md"
        self.source.parent.mkdir()
        self.source.write_text("# 正文\n\n首版内容。\n", encoding="utf-8")

    def article(self, **updates: object) -> NormalizedArticle:
        article = NormalizedArticle(
            key="sha256:1234567890abcdef",
            project="项目甲",
            account="创新观察",
            title="第一篇文章",
            published="2026-07-10",
            source_url="https://mp.weixin.qq.com/s/first",
            collected_at="2026-07-11T09:30:00+08:00",
            content_hash="sha256:body-v1",
            body="# 正文\n\n首版内容。\n",
            source_markdown=self.source,
        )
        return replace(article, **updates)

    def project_result(self, **updates: object) -> ProjectRunResult:
        result = ProjectRunResult(
            project="项目甲",
            account="创新观察",
            discovered=1,
            downloaded=1,
            skipped=0,
            failed=0,
            status="success",
            error="",
        )
        return replace(result, **updates)

    def manifest(self) -> dict[str, object]:
        return json.loads(
            (self.vault / "90-系统" / "manifest.json").read_text(encoding="utf-8")
        )

    def article_path(self, key: str = "sha256:1234567890abcdef") -> Path:
        record = self.manifest()["articles"][key]
        return self.vault / record["path"]

    def frontmatter(self, path: Path) -> dict[str, str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "---")
        closing = lines.index("---", 1)
        result = {}
        for line in lines[1:closing]:
            name, value = line.split(": ", 1)
            result[name] = json.loads(value)
        return result

    def test_first_apply_creates_article_and_second_apply_leaves_it_untouched(
        self,
    ) -> None:
        writer = VaultWriter(self.vault)
        article = self.article()

        first = writer.apply([article], [self.project_result()])

        self.assertEqual(first, VaultApplyResult(created=1, updated=0, unchanged=0))
        record = self.manifest()["articles"][article.key]
        self.assertEqual(record["key"], article.key)
        self.assertEqual(
            set(record),
            {
                "key",
                "project",
                "account",
                "title",
                "published",
                "source_url",
                "collected_at",
                "content_hash",
                "read_status",
                "path",
                "attachments",
            },
        )
        article_path = self.vault / record["path"]
        original_bytes = article_path.read_bytes()
        original_mtime = article_path.stat().st_mtime_ns
        self.assertIn("# 正文", original_bytes.decode("utf-8"))

        second = writer.apply([article], [self.project_result()])

        self.assertEqual(second, VaultApplyResult(created=0, updated=0, unchanged=1))
        self.assertEqual(article_path.read_bytes(), original_bytes)
        self.assertEqual(article_path.stat().st_mtime_ns, original_mtime)

    def test_changed_hash_updates_in_place_and_preserves_edited_read_status(self) -> None:
        writer = VaultWriter(self.vault)
        original = self.article()
        writer.apply([original], [self.project_result()])
        path = self.article_path()
        original_relative = path.relative_to(self.vault)
        edited = path.read_text(encoding="utf-8").replace(
            'read_status: "unread"', 'read_status: "已读"'
        )
        path.write_text(edited, encoding="utf-8")
        changed = self.article(
            title="改名后也不移动",
            content_hash="sha256:body-v2",
            body="# 正文\n\n第二版内容。\n",
        )

        result = writer.apply([changed], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        self.assertEqual(self.article_path().relative_to(self.vault), original_relative)
        self.assertEqual(self.frontmatter(path)["read_status"], "已读")
        self.assertEqual(
            self.manifest()["articles"][changed.key]["read_status"], "已读"
        )
        self.assertIn("第二版内容", path.read_text(encoding="utf-8"))

    def test_safe_filenames_and_json_frontmatter_never_leak_source_paths(self) -> None:
        title = ' 标题: "引号"\n下一行/\\*?<>|. ' + "超长中文" * 80
        project = "项目/甲:测试"
        article = self.article(project=project, title=title, key="not-a-sha-key")

        VaultWriter(self.vault).apply(
            [article], [self.project_result(project=project)]
        )

        record = self.manifest()["articles"][article.key]
        relative = Path(record["path"])
        self.assertFalse(relative.is_absolute())
        self.assertNotIn("..", relative.parts)
        self.assertFalse(any(character in relative.name for character in '/\\:*?"<>|'))
        self.assertFalse(relative.name.endswith((".", " ")))
        self.assertLessEqual(len(relative.name), 128)
        self.assertLessEqual(len(relative.name.encode("utf-8")), 255)
        frontmatter = self.frontmatter(self.vault / relative)
        self.assertEqual(frontmatter["title"], title)
        self.assertEqual(frontmatter["project"], project)
        self.assertEqual(frontmatter["read_status"], "unread")
        manifest_text = json.dumps(self.manifest(), ensure_ascii=False)
        self.assertNotIn("source_markdown", manifest_text)
        self.assertNotIn("source_image_dir", manifest_text)
        self.assertNotIn(str(self.root), manifest_text)
        self.assertNotIn("/Users/", manifest_text)

    def test_project_pages_include_zero_article_and_manifest_projects_and_sort_links(
        self,
    ) -> None:
        writer = VaultWriter(self.vault)
        old = self.article(project="旧项目", title="旧文章")
        writer.apply([old], [self.project_result(project="旧项目")])
        newer = self.article(
            key="sha256:aaaaaaaaaaaaaaaa",
            project="当前项目",
            title="较新文章",
            published="2026-07-11",
        )
        older = self.article(
            key="sha256:bbbbbbbbbbbbbbbb",
            project="当前项目",
            title="较早文章",
            published="2026-01-02",
        )
        results = [
            self.project_result(project="当前项目", discovered=2, downloaded=2),
            self.project_result(
                project="零文章项目", discovered=0, downloaded=0, skipped=0
            ),
        ]

        writer.apply([older, newer], results)

        project_directory = self.vault / "02-项目"
        self.assertTrue((project_directory / "旧项目.md").is_file())
        self.assertTrue((project_directory / "零文章项目.md").is_file())
        page = (project_directory / "当前项目.md").read_text(encoding="utf-8")
        self.assertLess(page.index("较新文章"), page.index("较早文章"))
        self.assertIn("[[../03-文章/", page)
        home = (self.vault / "00-首页.md").read_text(encoding="utf-8")
        self.assertIn("[[01-采集状态|采集状态]]", home)
        self.assertIn("总文章数：3", home)
        self.assertIn("零文章项目", home)

    def test_copies_only_safe_images_and_rewrites_only_existing_local_links(
        self,
    ) -> None:
        image_directory = self.root / "export" / "images" / "源 图"
        nested = image_directory / "nested"
        nested.mkdir(parents=True)
        (nested / "a b.png").write_bytes(b"png-data")
        (nested / "note.txt").write_text("not an image", encoding="utf-8")
        (image_directory / ".hidden.jpg").write_bytes(b"hidden")
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        (nested / "linked.jpg").symlink_to(outside)
        body = (
            "![本地](../images/%E6%BA%90%20%E5%9B%BE/nested/a%20b.png)\n"
            "![远程](https://example.com/remote.png)\n"
            "![缺失](../images/%E6%BA%90%20%E5%9B%BE/missing.jpg)\n"
        )
        article = self.article(body=body, source_image_dir=image_directory)

        VaultWriter(self.vault).apply([article], [self.project_result()])

        record = self.manifest()["articles"][article.key]
        self.assertEqual(len(record["attachments"]), 1)
        copied = self.vault / record["attachments"][0]
        self.assertEqual(copied.read_bytes(), b"png-data")
        attachment_root = copied.parents[1]
        self.assertEqual(
            sorted(path.name for path in attachment_root.rglob("*") if path.is_file()),
            ["a b.png"],
        )
        rendered = self.article_path().read_text(encoding="utf-8")
        self.assertIn("../../04-附件/项目甲/", rendered)
        self.assertIn("nested/a%20b.png", rendered)
        self.assertIn("https://example.com/remote.png", rendered)
        self.assertIn("../images/%E6%BA%90%20%E5%9B%BE/missing.jpg", rendered)

    def test_status_report_and_readme_cover_project_outcomes(self) -> None:
        results = [
            self.project_result(
                project="成功项目",
                discovered=3,
                downloaded=2,
                skipped=1,
            ),
            self.project_result(
                project="失败|项目",
                account="失败账号",
                discovered=4,
                downloaded=1,
                skipped=1,
                failed=2,
                status="failed",
                error="boom\n# injected | cell\x00",
            ),
        ]

        VaultWriter(self.vault).apply([], results)

        status = (self.vault / "01-采集状态.md").read_text(encoding="utf-8")
        for value in ("discovered", "downloaded", "skipped", "failed", "status", "error"):
            self.assertIn(value, status)
        self.assertIn("失败\\|项目", status)
        self.assertNotIn("boom\n# injected", status)
        self.assertNotIn("\x00", status)
        report = (self.vault / "90-系统" / "collection-report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("项目数：2", report)
        self.assertIn("失败项目数：1", report)
        self.assertIn("文章总数：0", report)
        self.assertIn("成功项目", report)
        self.assertIn("失败\\|项目", report)
        readme = (self.vault / "90-系统" / "README.md").read_text(encoding="utf-8")
        self.assertIn("Obsidian", readme)
        self.assertIn("read_status", readme)
        self.assertIn("90-系统", readme)

    def test_replace_failure_preserves_old_article_and_removes_temporary_file(
        self,
    ) -> None:
        writer = VaultWriter(self.vault)
        writer.apply([self.article()], [self.project_result()])
        path = self.article_path()
        original_bytes = path.read_bytes()
        changed = self.article(content_hash="sha256:changed", body="changed\n")

        with patch.object(vault_module.os, "replace", side_effect=OSError("disk")):
            with self.assertRaisesRegex(OSError, "disk"):
                writer.apply([changed], [self.project_result()])

        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(list(path.parent.glob(path.name + ".*.tmp")), [])
        self.assertEqual(
            self.manifest()["articles"][changed.key]["content_hash"],
            "sha256:body-v1",
        )

    def test_duplicate_key_uses_first_article_only(self) -> None:
        first = self.article(title="首个", body="first\n")
        duplicate = self.article(title="重复", body="second\n", content_hash="second")

        result = VaultWriter(self.vault).apply(
            [first, duplicate], [self.project_result()]
        )

        self.assertEqual(result, VaultApplyResult(created=1, updated=0, unchanged=0))
        rendered = self.article_path().read_text(encoding="utf-8")
        self.assertIn("first", rendered)
        self.assertNotIn("second", rendered)
        self.assertEqual(
            self.manifest()["articles"][first.key]["content_hash"],
            first.content_hash,
        )

    def test_unsafe_existing_manifest_path_is_rebuilt_inside_vault(self) -> None:
        writer = VaultWriter(self.vault)
        article = self.article()
        writer.apply([article], [self.project_result()])
        old_path = self.article_path()
        manifest_path = self.vault / "90-系统" / "manifest.json"
        manifest = self.manifest()
        manifest["articles"][article.key]["path"] = "../../escaped.md"
        manifest["articles"][article.key]["source_markdown"] = str(self.source)
        manifest["articles"][article.key]["source_image_dir"] = str(self.source.parent)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        old_path.unlink()

        result = writer.apply([article], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        rebuilt = self.article_path()
        self.assertTrue(rebuilt.is_relative_to(self.vault))
        self.assertIn("03-文章", rebuilt.parts)
        self.assertFalse((self.root / "escaped.md").exists())
        cleaned = self.manifest()["articles"][article.key]
        self.assertNotIn("source_markdown", cleaned)
        self.assertNotIn("source_image_dir", cleaned)


if __name__ == "__main__":
    unittest.main()
