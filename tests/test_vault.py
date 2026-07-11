from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
import unicodedata
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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

    def test_empty_apply_precreates_all_vault_directories(self) -> None:
        result = VaultWriter(self.vault).apply([], [])

        self.assertEqual(result, VaultApplyResult(created=0, updated=0, unchanged=0))
        for relative in ("02-项目", "03-文章", "04-附件", "90-系统"):
            with self.subTest(relative=relative):
                self.assertTrue((self.vault / relative).is_dir())

    def test_vault_lock_serializes_the_entire_apply_across_processes(self) -> None:
        self.vault.mkdir()
        lock_path = self.vault / ".vault.lock"
        command = [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "from inno_collector.vault import VaultWriter; "
                "VaultWriter(Path(sys.argv[1])).apply([], [])"
            ),
            str(self.vault),
        ]

        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.2)
                blocked = process.poll() is None
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=5)
        self.assertTrue(blocked)
        self.assertEqual(process.returncode, 0, (stdout, stderr))

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

    def test_same_hash_metadata_changes_update_article_frontmatter(self) -> None:
        writer = VaultWriter(self.vault)
        original = self.article()
        writer.apply([original], [self.project_result()])
        path = self.article_path()
        changed = replace(
            original,
            project="新项目",
            account="新账号",
            title="新标题",
            published="2026-07-11",
            source_url="https://mp.weixin.qq.com/s/metadata-change",
            collected_at="2026-07-12T10:00:00+08:00",
        )

        result = writer.apply(
            [changed],
            [self.project_result(project="新项目", account="新账号")],
        )

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        frontmatter = self.frontmatter(path)
        for field in (
            "project",
            "account",
            "title",
            "published",
            "source_url",
            "collected_at",
            "content_hash",
        ):
            with self.subTest(field=field):
                self.assertEqual(frontmatter[field], getattr(changed, field))
                self.assertEqual(
                    self.manifest()["articles"][changed.key][field],
                    getattr(changed, field),
                )

    def test_read_status_accepts_plain_scalar_and_rejects_yaml_structures(self) -> None:
        path = self.root / "status.md"
        path.write_text(
            "---\nread_status: 已读\n---\n\n正文\n",
            encoding="utf-8",
        )
        self.assertEqual(vault_module._read_status(path, "fallback"), "已读")

        unsafe_values = (
            "已读 # 注释",
            "[已读]",
            "{status: 已读}",
            "|",
            ">",
            "true",
            "123",
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                path.write_text(
                    f"---\nread_status: {value}\ncontinuation: injected\n---\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    vault_module._read_status(path, "fallback"),
                    "fallback",
                )
        path.write_text(
            "---\nread_status: 已读\n  多行续写\n---\n",
            encoding="utf-8",
        )
        self.assertEqual(vault_module._read_status(path, "fallback"), "fallback")

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

    def test_oversized_sha_like_key_is_hashed_to_a_safe_path_component(self) -> None:
        article = self.article(key="sha256:" + "a" * 1000)

        result = VaultWriter(self.vault).apply(
            [article], [self.project_result()]
        )

        self.assertEqual(result, VaultApplyResult(created=1, updated=0, unchanged=0))
        relative = Path(self.manifest()["articles"][article.key]["path"])
        self.assertLessEqual(len(relative.name.encode("utf-8")), 255)
        self.assertTrue((self.vault / relative).is_file())

    def test_obsidian_fragment_and_block_markers_are_removed_from_link_targets(self) -> None:
        project = "项目#片段^块"
        article = self.article(
            key="sha256:" + "d" * 64,
            project=project,
            title="标题#片段^块",
        )

        VaultWriter(self.vault).apply(
            [article], [self.project_result(project=project)]
        )

        record = self.manifest()["articles"][article.key]
        self.assertFalse(any(marker in record["path"] for marker in "#^"))
        rendered = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                self.vault / "00-首页.md",
                *(self.vault / "02-项目").glob("*.md"),
            )
        )
        targets = re.findall(r"\[\[([^]|]+)", rendered)
        self.assertTrue(targets)
        self.assertTrue(
            all(not any(marker in target for marker in "#^") for target in targets)
        )

    def test_default_article_and_attachment_names_use_identity_sha_first8(self) -> None:
        image_directory = self.root / "export" / "images" / "default-key"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"image")
        article = self.article(
            key="sha256:12345678" + "a" * 56,
            body="![a](../images/default-key/a.png)\n",
            source_image_dir=image_directory,
        )

        VaultWriter(self.vault).apply([article], [self.project_result()])

        record = self.manifest()["articles"][article.key]
        self.assertTrue(Path(record["path"]).stem.endswith("-12345678"))
        self.assertTrue(
            Path(record["attachments"][0]).parent.name.endswith("-12345678")
        )

    def test_long_title_article_can_be_updated_without_backup_name_overflow(self) -> None:
        article = self.article(
            key="sha256:" + "c" * 64,
            title="A" * 200 + "很长的中文标题" * 20,
        )
        writer = VaultWriter(self.vault)
        writer.apply([article], [self.project_result()])
        path = self.article_path(article.key)

        result = writer.apply(
            [replace(article, content_hash="sha256:changed", body="更新正文。\n")],
            [self.project_result()],
        )

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        self.assertIn("更新正文", path.read_text(encoding="utf-8"))
        self.assertEqual(list(path.parent.glob(".*.backup-*")), [])

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

    def test_project_page_cleanup_removes_only_stale_regular_markdown(self) -> None:
        writer = VaultWriter(self.vault)
        writer.apply(
            [self.article(project="旧项目")],
            [self.project_result(project="旧项目")],
        )
        project_directory = self.vault / "02-项目"
        stale = project_directory / "手工旧页.md"
        stale.write_text("stale", encoding="utf-8")
        keep_text = project_directory / "保留.txt"
        keep_text.write_text("keep", encoding="utf-8")
        keep_directory = project_directory / "目录.md"
        keep_directory.mkdir()
        external = self.root / "external.md"
        external.write_text("external", encoding="utf-8")
        keep_symlink = project_directory / "链接.md"
        keep_symlink.symlink_to(external)

        writer.apply(
            [self.article(project="新项目")],
            [self.project_result(project="新项目")],
        )

        self.assertFalse((project_directory / "旧项目.md").exists())
        self.assertFalse(stale.exists())
        self.assertTrue((project_directory / "新项目.md").is_file())
        self.assertTrue(keep_text.is_file())
        self.assertTrue(keep_directory.is_dir())
        self.assertTrue(keep_symlink.is_symlink())
        self.assertEqual(external.read_text(encoding="utf-8"), "external")

    def test_status_and_report_are_independent_of_project_result_order(self) -> None:
        first = self.project_result(
            project="乙项目",
            account="乙账号",
            discovered=3,
            downloaded=2,
            skipped=1,
        )
        second = self.project_result(
            project="甲项目",
            account="甲账号",
            discovered=4,
            downloaded=1,
            failed=3,
            status="failed",
            error="failure",
        )
        other_vault = self.root / "other-vault"

        VaultWriter(self.vault).apply([], [first, second])
        VaultWriter(other_vault).apply([], [second, first])

        for relative in ("01-采集状态.md", "90-系统/collection-report.md"):
            with self.subTest(relative=relative):
                self.assertEqual(
                    (self.vault / relative).read_bytes(),
                    (other_vault / relative).read_bytes(),
                )

    def test_zero_article_project_status_includes_account_last_sync(self) -> None:
        result = self.project_result(
            project="零文章项目",
            account="零文章账号",
            discovered=0,
            downloaded=0,
            last_sync="2026-07-14T08:09:10+08:00",
        )

        VaultWriter(self.vault).apply([], [result])

        status = (self.vault / "01-采集状态.md").read_text(encoding="utf-8")
        self.assertIn("last_sync", status)
        self.assertIn("2026-07-14T08:09:10+08:00", status)
        self.assertIn("零文章账号", status)
        home = (self.vault / "00-首页.md").read_text(encoding="utf-8")
        self.assertIn("最近更新时间：2026-07-14T08:09:10+08:00", home)

    def test_colliding_safe_project_names_get_unique_deterministic_pages(self) -> None:
        first = self.article(
            key="sha256:aaaaaaaaaaaaaaaa",
            project="A/B",
            title="斜杠项目文章",
        )
        second = self.article(
            key="sha256:bbbbbbbbbbbbbbbb",
            project="A:B",
            title="冒号项目文章",
        )
        results = [
            self.project_result(project="A/B"),
            self.project_result(project="A:B"),
        ]
        other_vault = self.root / "collision-other"

        VaultWriter(self.vault).apply([first, second], results)
        VaultWriter(other_vault).apply([second, first], list(reversed(results)))

        home = (self.vault / "00-首页.md").read_text(encoding="utf-8")
        targets = {
            project: re.search(
                rf"\[\[02-项目/([^|]+)\|{re.escape(project)}\]\]", home
            ).group(1)
            for project in ("A/B", "A:B")
        }
        self.assertEqual(len(set(targets.values())), 2)
        slash_page = (self.vault / "02-项目" / f"{targets['A/B']}.md").read_text(
            encoding="utf-8"
        )
        colon_page = (self.vault / "02-项目" / f"{targets['A:B']}.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("斜杠项目文章", slash_page)
        self.assertNotIn("冒号项目文章", slash_page)
        self.assertIn("冒号项目文章", colon_page)
        self.assertNotIn("斜杠项目文章", colon_page)
        self.assertEqual(
            (self.vault / "00-首页.md").read_bytes(),
            (other_vault / "00-首页.md").read_bytes(),
        )
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (self.vault / "02-项目").glob("*.md")
            },
            {
                path.name: path.read_bytes()
                for path in (other_vault / "02-项目").glob("*.md")
            },
        )

    def test_project_pages_are_unique_under_casefold_and_nfc_semantics(self) -> None:
        projects = ["Foo", "foo", "Caf\u00e9", "Cafe\u0301"]
        results = [self.project_result(project=project) for project in projects]
        other_vault = self.root / "casefold-other"

        VaultWriter(self.vault).apply([], results)
        VaultWriter(other_vault).apply([], list(reversed(results)))

        pages = sorted(path.name for path in (self.vault / "02-项目").glob("*.md"))
        semantic_names = {
            unicodedata.normalize("NFC", page).casefold() for page in pages
        }
        self.assertEqual(len(pages), len(projects))
        self.assertEqual(len(semantic_names), len(projects))
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (self.vault / "02-项目").glob("*.md")
            },
            {
                path.name: path.read_bytes()
                for path in (other_vault / "02-项目").glob("*.md")
            },
        )

    def test_copies_only_safe_images_and_rewrites_existing_local_links(
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

    def test_rewrites_exporter_image_links_with_unescaped_spaces(self) -> None:
        image_directory = self.root / "export" / "images" / "乐新闻 _ 标题"
        image_directory.mkdir(parents=True)
        (image_directory / "001.gif").write_bytes(b"gif-data")
        article = self.article(
            body="![image](../images/乐新闻 _ 标题/001.gif)\n",
            source_image_dir=image_directory,
        )

        VaultWriter(self.vault).apply([article], [self.project_result()])

        rendered = self.article_path().read_text(encoding="utf-8")
        self.assertNotIn("../images/", rendered)
        self.assertIn("../../04-附件/项目甲/", rendered)
        self.assertIn("/001.gif", rendered)

    def test_rewritten_attachment_links_encode_nonbreaking_spaces(self) -> None:
        image_directory = self.root / "export" / "images" / "标题\u00a0后半"
        image_directory.mkdir(parents=True)
        (image_directory / "001.gif").write_bytes(b"gif-data")
        article = self.article(
            title="标题\u00a0后半",
            body="![image](../images/标题\u00a0后半/001.gif)\n",
            source_image_dir=image_directory,
        )

        VaultWriter(self.vault).apply([article], [self.project_result()])

        rendered = self.article_path().read_text(encoding="utf-8")
        self.assertIn("%C2%A0", rendered)
        image_line = next(line for line in rendered.splitlines() if line.startswith("!["))
        self.assertNotIn("\u00a0", image_line)

    def test_same_hash_late_images_update_body_once_then_stay_unchanged(self) -> None:
        body = "![后补](../images/later/a.png)\n"
        article = self.article(body="正文等待后补。\n", source_image_dir=None)
        writer = VaultWriter(self.vault)
        writer.apply([article], [self.project_result()])
        path = self.article_path()
        before = path.read_bytes()
        image_directory = self.root / "export" / "images" / "later"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"late-image")

        updated = writer.apply(
            [replace(article, body=body, source_image_dir=image_directory)],
            [self.project_result()],
        )

        self.assertEqual(updated, VaultApplyResult(created=0, updated=1, unchanged=0))
        self.assertNotEqual(path.read_bytes(), before)
        self.assertIn("../../04-附件/", path.read_text(encoding="utf-8"))
        updated_mtime = path.stat().st_mtime_ns

        unchanged = writer.apply(
            [replace(article, body=body, source_image_dir=image_directory)],
            [self.project_result()],
        )

        self.assertEqual(
            unchanged, VaultApplyResult(created=0, updated=0, unchanged=1)
        )
        self.assertEqual(path.stat().st_mtime_ns, updated_mtime)

    def test_same_hash_and_images_do_not_replace_attachment_files(self) -> None:
        image_directory = self.root / "export" / "images" / "stable-assets"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"stable")
        article = self.article(
            body="![a](../images/stable-assets/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([article], [self.project_result()])
        attachment = self.vault / self.manifest()["articles"][article.key][
            "attachments"
        ][0]
        before = attachment.stat()

        result = writer.apply([article], [self.project_result()])

        after = attachment.stat()
        self.assertEqual(result, VaultApplyResult(created=0, updated=0, unchanged=1))
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_hash_change_without_source_images_keeps_existing_vault_links(self) -> None:
        image_directory = self.root / "export" / "images" / "existing"
        image_directory.mkdir(parents=True)
        (image_directory / "nested").mkdir()
        (image_directory / "nested" / "a.png").write_bytes(b"existing-image")
        original = self.article(
            body="![现有](../images/existing/nested/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result()])
        self.assertIn(
            "../../04-附件/", self.article_path().read_text(encoding="utf-8")
        )

        changed = replace(
            original,
            content_hash="sha256:new-body",
            body="新增正文。\n\n![现有](../images/renamed-source/nested/a.png)\n",
            source_image_dir=None,
        )
        result = writer.apply([changed], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        rendered = self.article_path().read_text(encoding="utf-8")
        self.assertIn("新增正文", rendered)
        self.assertIn("../../04-附件/", rendered)
        self.assertNotIn("../images/renamed-source", rendered)
        attachment = self.vault / self.manifest()["articles"][changed.key][
            "attachments"
        ][0]
        self.assertEqual(attachment.read_bytes(), b"existing-image")

    def test_temporarily_missing_image_source_preserves_previous_snapshot(self) -> None:
        image_directory = self.root / "export" / "images" / "temporary"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"previous-image")
        original = self.article(
            body="![a](../images/temporary/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result()])
        old_attachment = self.vault / self.manifest()["articles"][original.key][
            "attachments"
        ][0]
        (image_directory / "a.png").unlink()
        image_directory.rmdir()
        changed = replace(
            original,
            content_hash="sha256:source-temporary-missing",
            body="更新正文。\n\n![a](../images/temporary/a.png)\n",
        )

        result = writer.apply([changed], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        record = self.manifest()["articles"][changed.key]
        self.assertEqual(len(record["attachments"]), 1)
        self.assertEqual((self.vault / record["attachments"][0]), old_attachment)
        self.assertEqual(old_attachment.read_bytes(), b"previous-image")
        rendered = self.article_path().read_text(encoding="utf-8")
        self.assertIn("../../04-附件/", rendered)
        report = (self.vault / "90-系统" / "collection-report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("附件", report)
        self.assertIn("保留", report)

    def test_single_image_copy_failure_with_unresolved_reference_rolls_back(self) -> None:
        image_directory = self.root / "export" / "images" / "partial-copy"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"old-a")
        original = self.article(
            body="![a](../images/partial-copy/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result()])
        manifest_path = self.vault / "90-系统" / "manifest.json"
        old_manifest = manifest_path.read_bytes()
        article_path = self.article_path()
        old_article = article_path.read_bytes()
        old_attachment = self.vault / self.manifest()["articles"][original.key][
            "attachments"
        ][0]
        (image_directory / "a.png").write_bytes(b"new-a")
        (image_directory / "b.png").write_bytes(b"new-b")
        changed = replace(
            original,
            content_hash="sha256:partial-copy",
            body=(
                "![a](../images/partial-copy/a.png)\n"
                "![b](../images/partial-copy/b.png)\n"
                "![remote](https://example.com/image.png)\n"
            ),
        )
        original_copy = vault_module._atomic_copy

        def fail_b(source: Path, destination: Path) -> None:
            if source.name == "b.png":
                raise OSError("/Users/private/export/b.png unavailable")
            original_copy(source, destination)

        with patch.object(vault_module, "_atomic_copy", side_effect=fail_b):
            with self.assertRaisesRegex(
                vault_module.AttachmentSyncError,
                "^unresolved local image references$",
            ):
                writer.apply([changed], [self.project_result()])

        self.assertEqual(manifest_path.read_bytes(), old_manifest)
        self.assertEqual(article_path.read_bytes(), old_article)
        self.assertEqual(old_attachment.read_bytes(), b"old-a")

    def test_missing_image_source_with_partial_old_mapping_rolls_back(self) -> None:
        image_directory = self.root / "export" / "images" / "old-snapshot"
        image_directory.mkdir(parents=True)
        (image_directory / "known.png").write_bytes(b"known")
        original = self.article(
            body="![known](../images/old-snapshot/known.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result()])
        manifest_path = self.vault / "90-系统" / "manifest.json"
        old_manifest = manifest_path.read_bytes()
        article_path = self.article_path()
        old_article = article_path.read_bytes()
        old_attachment = self.vault / self.manifest()["articles"][original.key][
            "attachments"
        ][0]
        changed = replace(
            original,
            content_hash="sha256:missing-source-partial-old-mapping",
            body=(
                "![known](../images/old-snapshot/known.png)\n"
                "![unknown](../images/old-snapshot/unknown.png)\n"
            ),
            source_image_dir=None,
        )

        with self.assertRaisesRegex(
            vault_module.AttachmentSyncError,
            "^unresolved local image references$",
        ):
            writer.apply([changed], [self.project_result()])

        self.assertEqual(manifest_path.read_bytes(), old_manifest)
        self.assertEqual(article_path.read_bytes(), old_article)
        self.assertEqual(old_attachment.read_bytes(), b"known")

    def test_new_article_without_image_source_rejects_encoded_local_reference(
        self,
    ) -> None:
        article = self.article(
            body=(
                "![missing](../images/%E6%BA%90%20%E7%9B%AE%E5%BD%95/a%20b.png)\n"
                "![remote](https://example.com/image.png)\n"
            ),
            source_image_dir=None,
        )

        with self.assertRaisesRegex(
            vault_module.AttachmentSyncError,
            "^unresolved local image references$",
        ):
            VaultWriter(self.vault).apply([article], [self.project_result()])

        self.assertFalse((self.vault / "90-系统" / "manifest.json").exists())
        self.assertEqual(list((self.vault / "03-文章").rglob("*.md")), [])
        self.assertEqual(list((self.vault / "04-附件").rglob("*")), [])

    def test_new_article_with_empty_image_source_rejects_missing_reference(
        self,
    ) -> None:
        image_directory = self.root / "export" / "images" / "empty-source"
        image_directory.mkdir(parents=True)
        article = self.article(
            body="![missing](../images/empty-source/missing.png)\n",
            source_image_dir=image_directory,
        )

        with self.assertRaisesRegex(
            vault_module.AttachmentSyncError,
            "^unresolved local image references$",
        ):
            VaultWriter(self.vault).apply([article], [self.project_result()])

        self.assertFalse((self.vault / "90-系统" / "manifest.json").exists())
        self.assertEqual(list((self.vault / "03-文章").rglob("*.md")), [])
        self.assertEqual(list((self.vault / "04-附件").rglob("*")), [])

    def test_new_article_rolls_back_partial_snapshot_when_one_reference_is_missing(
        self,
    ) -> None:
        image_directory = self.root / "export" / "images" / "partial-source"
        image_directory.mkdir(parents=True)
        (image_directory / "present.png").write_bytes(b"present")
        article = self.article(
            body=(
                "![present](../images/partial-source/present.png)\n"
                "![missing](../images/partial-source/missing%20image.png)\n"
            ),
            source_image_dir=image_directory,
        )

        with self.assertRaisesRegex(
            vault_module.AttachmentSyncError,
            "^unresolved local image references$",
        ):
            VaultWriter(self.vault).apply([article], [self.project_result()])

        self.assertFalse((self.vault / "90-系统" / "manifest.json").exists())
        self.assertEqual(list((self.vault / "03-文章").rglob("*.md")), [])
        self.assertEqual(list((self.vault / "04-附件").rglob("*")), [])

    def test_remote_image_reference_does_not_require_local_snapshot(self) -> None:
        article = self.article(
            body="![remote](https://example.com/image.png)\n",
            source_image_dir=None,
        )

        result = VaultWriter(self.vault).apply([article], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=1, updated=0, unchanged=0))
        self.assertIn(
            "https://example.com/image.png",
            self.article_path().read_text(encoding="utf-8"),
        )

    def test_new_article_attachment_failure_is_rejected_without_any_artifacts(
        self,
    ) -> None:
        image_directory = self.root / "export" / "images" / "batch"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"a")
        (image_directory / "b.png").write_bytes(b"b")
        article = self.article(
            body=(
                "![a](../images/batch/a.png)\n"
                "![b](../images/batch/b.png)\n"
            ),
            source_image_dir=image_directory,
        )
        original_copy = vault_module._atomic_copy
        calls = 0

        def fail_second(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second copy failed")
            original_copy(source, destination)

        with patch.object(vault_module, "_atomic_copy", side_effect=fail_second):
            with self.assertRaisesRegex(
                vault_module.AttachmentSyncError,
                "^attachment sync failed without previous snapshot$",
            ):
                VaultWriter(self.vault).apply(
                    [article], [self.project_result()]
                )

        manifest_path = self.vault / "90-系统" / "manifest.json"
        self.assertFalse(manifest_path.exists())
        project_assets = self.vault / "04-附件" / "项目甲"
        self.assertEqual(list(project_assets.iterdir()) if project_assets.exists() else [], [])
        self.assertEqual(list((self.vault / "03-文章").rglob("*.md")), [])

    def test_attachment_snapshot_replacement_physically_removes_old_files(self) -> None:
        image_directory = self.root / "export" / "images" / "snapshot"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"a-v1")
        (image_directory / "b.png").write_bytes(b"b-v1")
        article = self.article(
            body=(
                "![a](../images/snapshot/a.png)\n"
                "![b](../images/snapshot/b.png)\n"
            ),
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([article], [self.project_result()])
        first_attachments = self.manifest()["articles"][article.key]["attachments"]
        old_b = self.vault / next(
            attachment for attachment in first_attachments if attachment.endswith("b.png")
        )
        self.assertTrue(old_b.is_file())
        (image_directory / "a.png").write_bytes(b"a-v2")
        (image_directory / "b.png").unlink()
        changed = replace(
            article,
            content_hash="sha256:snapshot-v2",
            body="![a](../images/snapshot/a.png)\n",
        )

        result = writer.apply([changed], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        attachments = self.manifest()["articles"][article.key]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0].endswith("a.png"))
        self.assertEqual((self.vault / attachments[0]).read_bytes(), b"a-v2")
        self.assertFalse(old_b.exists())
        asset_root = (self.vault / attachments[0]).parent
        self.assertEqual(
            sorted(path.name for path in asset_root.iterdir()),
            ["a.png"],
        )

    def test_project_and_title_change_removes_previous_attachment_root(self) -> None:
        image_directory = self.root / "export" / "images" / "rename"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"old")
        original = self.article(
            project="旧项目",
            title="旧标题",
            body="![a](../images/rename/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result(project="旧项目")])
        old_attachment = self.vault / self.manifest()["articles"][original.key][
            "attachments"
        ][0]
        old_root = old_attachment.parent
        (image_directory / "a.png").write_bytes(b"new")
        changed = replace(
            original,
            project="新项目",
            title="新标题",
            content_hash="sha256:renamed",
        )

        result = writer.apply(
            [changed], [self.project_result(project="新项目")]
        )

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        new_attachment = self.vault / self.manifest()["articles"][changed.key][
            "attachments"
        ][0]
        self.assertNotEqual(new_attachment.parent, old_root)
        self.assertEqual(new_attachment.read_bytes(), b"new")
        self.assertFalse(old_root.exists())

    def test_attachment_swap_refuses_preexisting_symlink_backup(self) -> None:
        image_directory = self.root / "export" / "images" / "backup"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"old")
        article = self.article(
            body="![a](../images/backup/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([article], [self.project_result()])
        attachment = self.vault / self.manifest()["articles"][article.key][
            "attachments"
        ][0]
        asset_root = attachment.parent
        old_bytes = attachment.read_bytes()
        external = self.root / "external-backup"
        external.mkdir()
        backup_digest = hashlib.sha256(asset_root.name.encode("utf-8")).hexdigest()[:12]
        backup = asset_root.parent / f".v.backup-{backup_digest}-fixed"
        backup.symlink_to(external, target_is_directory=True)
        (image_directory / "a.png").write_bytes(b"new")

        with patch.object(
            vault_module.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="fixed"),
        ):
            with self.assertRaisesRegex(ValueError, "unsafe attachment backup"):
                writer.apply(
                    [replace(article, content_hash="sha256:new")],
                    [self.project_result()],
                )

        self.assertTrue(backup.is_symlink())
        self.assertEqual(attachment.read_bytes(), old_bytes)
        self.assertEqual(list(external.iterdir()), [])

    def test_article_write_failure_rolls_back_renamed_attachment_snapshot(self) -> None:
        image_directory = self.root / "export" / "images" / "article-failure"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"old-attachment")
        original = self.article(
            project="旧项目",
            title="旧标题",
            body="![a](../images/article-failure/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result(project="旧项目")])
        article_path = self.article_path()
        old_article_bytes = article_path.read_bytes()
        manifest_path = self.vault / "90-系统" / "manifest.json"
        old_manifest_bytes = manifest_path.read_bytes()
        old_attachment = self.vault / self.manifest()["articles"][original.key][
            "attachments"
        ][0]
        old_attachment_bytes = old_attachment.read_bytes()
        (image_directory / "a.png").write_bytes(b"new-attachment")
        changed = replace(
            original,
            project="新项目",
            title="新标题",
            content_hash="sha256:article-failure",
        )
        original_atomic_write = vault_module._atomic_write

        def fail_article(path: Path, payload: bytes) -> None:
            if path.resolve() == article_path.resolve():
                raise OSError("article write failed")
            original_atomic_write(path, payload)

        with patch.object(vault_module, "_atomic_write", side_effect=fail_article):
            with self.assertRaisesRegex(OSError, "article write failed"):
                writer.apply(
                    [changed], [self.project_result(project="新项目")]
                )

        self.assertEqual(article_path.read_bytes(), old_article_bytes)
        self.assertEqual(manifest_path.read_bytes(), old_manifest_bytes)
        self.assertEqual(old_attachment.read_bytes(), old_attachment_bytes)
        new_project_assets = self.vault / "04-附件" / "新项目"
        self.assertEqual(
            list(new_project_assets.iterdir()) if new_project_assets.exists() else [],
            [],
        )
        hidden = list((self.vault / "04-附件").rglob(".*.stage-*"))
        hidden.extend((self.vault / "04-附件").rglob(".*.backup-*"))
        self.assertEqual(hidden, [])

    def test_manifest_save_failure_rolls_back_article_and_attachments(self) -> None:
        image_directory = self.root / "export" / "images" / "manifest-failure"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"old-attachment")
        original = self.article(
            body="旧正文。\n\n![a](../images/manifest-failure/a.png)\n",
            source_image_dir=image_directory,
        )
        writer = VaultWriter(self.vault)
        writer.apply([original], [self.project_result()])
        article_path = self.article_path()
        old_article_bytes = article_path.read_bytes()
        manifest_path = self.vault / "90-系统" / "manifest.json"
        old_manifest_bytes = manifest_path.read_bytes()
        attachment = self.vault / self.manifest()["articles"][original.key][
            "attachments"
        ][0]
        old_attachment_bytes = attachment.read_bytes()
        (image_directory / "a.png").write_bytes(b"new-attachment")
        changed = replace(
            original,
            content_hash="sha256:manifest-failure",
            body="新正文。\n\n![a](../images/manifest-failure/a.png)\n",
        )

        with patch.object(
            vault_module.ManifestStore,
            "save",
            side_effect=OSError("manifest save failed"),
        ):
            with self.assertRaisesRegex(OSError, "manifest save failed"):
                writer.apply([changed], [self.project_result()])

        self.assertEqual(article_path.read_bytes(), old_article_bytes)
        self.assertEqual(manifest_path.read_bytes(), old_manifest_bytes)
        self.assertEqual(attachment.read_bytes(), old_attachment_bytes)
        hidden = list((self.vault / "04-附件").rglob(".*.stage-*"))
        hidden.extend((self.vault / "04-附件").rglob(".*.backup-*"))
        self.assertEqual(hidden, [])

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

    def test_home_and_status_show_sync_recents_and_partial_failure(self) -> None:
        article = self.article(
            title="最近一篇",
            collected_at="2026-07-13T11:22:33+08:00",
        )
        failed = self.project_result(
            status="failed",
            failed=1,
            error="cannot read /Users/alice/private/export.md",
        )

        VaultWriter(self.vault).apply([article], [failed])

        home = (self.vault / "00-首页.md").read_text(encoding="utf-8")
        self.assertIn("最近更新时间：2026-07-13T11:22:33+08:00", home)
        self.assertIn("## 最近文章", home)
        self.assertIn("最近一篇", home)
        self.assertIn("局部失败", home)
        status = (self.vault / "01-采集状态.md").read_text(encoding="utf-8")
        self.assertIn("最后同步时间：2026-07-13T11:22:33+08:00", status)
        self.assertNotIn("/Users/alice", status)
        report = (self.vault / "90-系统" / "collection-report.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/Users/alice", report)

    def test_wikilink_aliases_collapse_controls_and_neutralize_delimiters(self) -> None:
        project = "项目|坏]]\n# 项目注入"
        title = "标题|坏]]\x00\n# 标题注入"
        article = self.article(project=project, title=title)

        VaultWriter(self.vault).apply(
            [article], [self.project_result(project=project)]
        )

        home = (self.vault / "00-首页.md").read_text(encoding="utf-8")
        project_pages = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.vault / "02-项目").glob("*.md")
        )
        for rendered in (home, project_pages):
            self.assertNotIn("|坏]]", rendered)
            self.assertNotIn("\n# 项目注入", rendered)
            self.assertNotIn("\n# 标题注入", rendered)
            self.assertNotIn("\x00", rendered)
        self.assertIn("｜", home + project_pages)
        self.assertIn("］］", home + project_pages)

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

    def test_full_keys_with_same_prefix_get_unique_order_independent_paths(self) -> None:
        image_directory = self.root / "export" / "images" / "key-collision"
        image_directory.mkdir(parents=True)
        (image_directory / "a.png").write_bytes(b"image")
        first = self.article(
            key="sha256:deadbeef" + "0" * 56,
            title="相同标题",
            source_url="https://mp.weixin.qq.com/s/key-first",
            body="![a](../images/key-collision/a.png)\n",
            source_image_dir=image_directory,
        )
        second = replace(
            first,
            key="sha256:deadbeef" + "1" * 56,
            source_url="https://mp.weixin.qq.com/s/key-second",
        )
        other_vault = self.root / "key-order-other"

        VaultWriter(self.vault).apply([first, second], [self.project_result()])
        VaultWriter(other_vault).apply(
            [second, first], [self.project_result()]
        )

        records = self.manifest()["articles"]
        paths = {record["path"] for record in records.values()}
        attachment_roots = {
            str(Path(record["attachments"][0]).parent)
            for record in records.values()
        }
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(attachment_roots), 2)
        self.assertEqual(
            {Path(path).stem.rsplit("-", 1)[-1] for path in paths},
            {"deadbeef0", "deadbeef1"},
        )
        self.assertEqual(
            (self.vault / "90-系统" / "manifest.json").read_bytes(),
            (other_vault / "90-系统" / "manifest.json").read_bytes(),
        )

    def test_existing_manifest_path_collision_is_reassigned_before_writes(self) -> None:
        first = self.article(
            key="sha256:" + "a" * 64,
            title="碰撞文章",
            source_url="https://mp.weixin.qq.com/s/collision-first",
        )
        second = replace(
            first,
            key="sha256:" + "b" * 64,
            source_url="https://mp.weixin.qq.com/s/collision-second",
        )
        writer = VaultWriter(self.vault)
        writer.apply([first, second], [self.project_result()])
        manifest_path = self.vault / "90-系统" / "manifest.json"
        manifest = self.manifest()
        shared_path = manifest["articles"][first.key]["path"]
        manifest["articles"][second.key]["path"] = shared_path
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

        writer.apply([second, first], [self.project_result()])

        records = self.manifest()["articles"]
        self.assertNotEqual(records[first.key]["path"], records[second.key]["path"])
        for article in (first, second):
            with self.subTest(key=article.key):
                metadata = self.frontmatter(self.vault / records[article.key]["path"])
                self.assertEqual(metadata["source_url"], article.source_url)

    def test_full_collision_refetch_removes_unreferenced_shared_article_only(
        self,
    ) -> None:
        first = self.article(
            key="sha256:" + "c" * 64,
            title="共享旧文章",
            source_url="https://mp.weixin.qq.com/s/shared-first",
        )
        second = replace(
            first,
            key="sha256:" + "d" * 64,
            source_url="https://mp.weixin.qq.com/s/shared-second",
        )
        writer = VaultWriter(self.vault)
        writer.apply([first, second], [self.project_result()])
        manifest_path = self.vault / "90-系统" / "manifest.json"
        manifest = self.manifest()
        shared_relative = "03-文章/项目甲/legacy-shared.md"
        shared_path = self.vault / shared_relative
        shared_path.write_bytes(
            (self.vault / manifest["articles"][first.key]["path"]).read_bytes()
        )
        manual_path = shared_path.with_name("manual-note.md")
        manual_path.write_text("manual", encoding="utf-8")
        for article in (first, second):
            manifest["articles"][article.key]["path"] = shared_relative
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        writer.apply(
            [
                replace(first, content_hash="sha256:shared-first-v2"),
                replace(second, content_hash="sha256:shared-second-v2"),
            ],
            [self.project_result()],
        )

        records = self.manifest()["articles"]
        self.assertNotEqual(records[first.key]["path"], records[second.key]["path"])
        self.assertFalse(shared_path.exists())
        self.assertEqual(manual_path.read_text(encoding="utf-8"), "manual")
        for article in (first, second):
            self.assertTrue((self.vault / records[article.key]["path"]).is_file())

    def test_manifest_collision_without_incoming_keeps_disk_and_requests_refetch(
        self,
    ) -> None:
        first = self.article(
            key="sha256:" + "1" * 64,
            title="旧碰撞",
            source_url="https://mp.weixin.qq.com/s/old-first",
        )
        second = replace(
            first,
            key="sha256:" + "2" * 64,
            source_url="https://mp.weixin.qq.com/s/old-second",
        )
        writer = VaultWriter(self.vault)
        writer.apply([first, second], [self.project_result()])
        manifest_path = self.vault / "90-系统" / "manifest.json"
        manifest = self.manifest()
        shared_relative = manifest["articles"][first.key]["path"]
        manifest["articles"][second.key]["path"] = shared_relative
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        manifest_bytes = manifest_path.read_bytes()
        shared_path = self.vault / shared_relative
        shared_bytes = shared_path.read_bytes()

        with self.assertRaisesRegex(
            vault_module.ManifestPathCollisionError,
            "^manifest path collision requires refetch$",
        ):
            writer.apply([], [])

        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(shared_path.read_bytes(), shared_bytes)

    def test_manifest_collision_migrates_only_incoming_article_transactionally(
        self,
    ) -> None:
        first = self.article(
            key="sha256:" + "3" * 64,
            title="部分碰撞",
            source_url="https://mp.weixin.qq.com/s/partial-first",
        )
        second = replace(
            first,
            key="sha256:" + "4" * 64,
            source_url="https://mp.weixin.qq.com/s/partial-second",
        )
        writer = VaultWriter(self.vault)
        writer.apply([first, second], [self.project_result()])
        manifest_path = self.vault / "90-系统" / "manifest.json"
        manifest = self.manifest()
        first_relative = manifest["articles"][first.key]["path"]
        second_original = self.vault / manifest["articles"][second.key]["path"]
        second_original.unlink()
        manifest["articles"][second.key]["path"] = first_relative
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        first_path = self.vault / first_relative
        first_bytes = first_path.read_bytes()
        changed_second = replace(
            second,
            content_hash="sha256:partial-refetched",
            body="重抓后的第二篇。\n",
        )

        result = writer.apply([changed_second], [self.project_result()])

        self.assertEqual(result, VaultApplyResult(created=0, updated=1, unchanged=0))
        records = self.manifest()["articles"]
        self.assertEqual(records[first.key]["path"], first_relative)
        self.assertNotEqual(records[second.key]["path"], first_relative)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        second_path = self.vault / records[second.key]["path"]
        self.assertIn("重抓后的第二篇", second_path.read_text(encoding="utf-8"))

    def test_distinct_full_keys_that_normalize_to_same_stem_are_preallocated(self) -> None:
        lower = self.article(
            key="sha256:" + "abcdef" * 10 + "abcd",
            title="规范化碰撞",
            source_url="https://mp.weixin.qq.com/s/lower-key",
        )
        upper = replace(
            lower,
            key=lower.key.upper().replace("SHA256:", "sha256:"),
            source_url="https://mp.weixin.qq.com/s/upper-key",
        )
        other_vault = self.root / "normalized-key-other"

        VaultWriter(self.vault).apply([upper, lower], [self.project_result()])
        VaultWriter(other_vault).apply([lower, upper], [self.project_result()])

        records = self.manifest()["articles"]
        self.assertEqual(len({record["path"] for record in records.values()}), 2)
        for article in (lower, upper):
            with self.subTest(key=article.key):
                path = self.vault / records[article.key]["path"]
                self.assertTrue(path.is_file())
                self.assertEqual(
                    self.frontmatter(path)["source_url"], article.source_url
                )
        self.assertEqual(
            (self.vault / "90-系统" / "manifest.json").read_bytes(),
            (other_vault / "90-系统" / "manifest.json").read_bytes(),
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
