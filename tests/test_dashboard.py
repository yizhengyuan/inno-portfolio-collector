from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from inno_collector.dashboard import build_dashboard
from inno_collector.identity import article_key
from inno_collector.models import NormalizedArticle, ProjectRunResult
from inno_collector.update_package import build_update_package
from inno_collector.vault import VaultWriter


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.vault = self.root / "vault"
        body = "# 正文\n\n看板测试正文。\n"
        source = self.root / "source.md"
        source.write_text(body, encoding="utf-8")
        article = NormalizedArticle(
            key=article_key("https://mp.weixin.qq.com/s/dashboard"),
            project="项目<script>alert(1)</script>",
            account="账号甲",
            title="文章 <看板>",
            published="2026-07-11",
            source_url="https://mp.weixin.qq.com/s/dashboard",
            collected_at="2026-07-11T12:00:00+08:00",
            content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            body=body,
            source_markdown=source,
        )
        result = ProjectRunResult(
            project=article.project,
            account=article.account,
            discovered=1,
            downloaded=1,
            skipped=0,
            failed=0,
            status="success",
            error="",
        )
        VaultWriter(self.vault).apply([article], [result])

    def test_dashboard_is_deterministic_self_contained_and_escaped(self) -> None:
        first = build_dashboard(self.vault).read_bytes()
        second = build_dashboard(self.vault).read_bytes()
        text = second.decode("utf-8")

        self.assertEqual(first, second)
        self.assertIn("离线资讯看板", text)
        self.assertIn("文章数", text)
        self.assertIn('id="search"', text)
        self.assertIn('id="project-filter"', text)
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertNotIn("<script src=", text)
        self.assertNotIn("<link href=", text)
        self.assertNotIn("fetch(", text)
        self.assertNotIn("@import", text)

    def test_dashboard_reflects_partial_collection_summary(self) -> None:
        result = ProjectRunResult(
            project="项目乙",
            account="账号乙",
            discovered=1,
            downloaded=0,
            skipped=0,
            failed=1,
            status="partial",
            error="一篇失败",
        )
        VaultWriter(self.vault).apply([], [result])

        text = build_dashboard(self.vault).read_text(encoding="utf-8")

        self.assertIn("部分成功", text)
        self.assertIn('"failedProjects":1', text)

    def test_update_package_refreshes_and_includes_dashboard(self) -> None:
        output = self.root / "baseline.inno-update"

        build_update_package(
            self.vault,
            output,
            created_at="2026-07-11T13:00:00Z",
        )

        with zipfile.ZipFile(output) as archive:
            self.assertIn(
                "payload/80-离线看板/index.html",
                archive.namelist(),
            )


if __name__ == "__main__":
    unittest.main()
