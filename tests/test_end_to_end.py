from __future__ import annotations

import csv
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from inno_collector.cli import build_parser
from inno_collector.config import load_projects
from inno_collector.package import build_delivery_zip, lint_vault
from inno_collector.pipeline import CollectionPipeline


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
REPOSITORY = Path(__file__).resolve().parents[1]


class OfflineExporter:
    def __init__(self, projects: tuple) -> None:
        self.projects = projects
        self.download_calls: list[tuple[int, ...]] = []
        self.rows: dict[int, list[dict]] = {}
        for index, _project in enumerate(projects, start=1):
            slug = "portfolio-1" if index == 2 else f"portfolio-{index}"
            title = "项目资讯 1" if index == 2 else f"项目资讯 {index}"
            self.rows[index] = [
                {
                    "id": index * 100,
                    "url": f"https://mp.weixin.qq.com/s/{slug}",
                    "publish_time": (
                        "2026-01-15 09:00:00"
                        if index == 2
                        else f"2026-0{(index - 1) % 9 + 1}-15 09:00:00"
                    ),
                    "title": title,
                }
            ]
        self.rows[1].append(
            {
                "id": 102,
                "url": "https://mp.weixin.qq.com/s/portfolio-extra",
                "publish_time": "2026-06-30 09:00:00",
                "title": "项目额外资讯",
            }
        )
        self.rows[1].append(
            {
                "id": 101,
                "url": "https://mp.weixin.qq.com/s/old-2025-article",
                "publish_time": "2025-12-31 23:59:59",
                "title": "旧资讯 2025",
            }
        )

    def auth_check(self) -> dict:
        return {"ok": True, "status": "valid"}

    def accounts(self) -> list[dict]:
        return [
            {"id": index, "nickname": project.account, "alias": project.wechat_id}
            for index, project in enumerate(self.projects, start=1)
        ]

    def resolve_exact(self, project, rows: list[dict]) -> dict:
        return next(row for row in rows if row["nickname"] == project.account)

    def sync(self, account_id: int, limit: int = 1000) -> dict:
        return {"ok": True}

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
        return self.rows[account_id]

    def download(self, article_ids: list[int], output_root: Path) -> dict:
        self.download_calls.append(tuple(article_ids))
        output = output_root / "account"
        output.mkdir()
        selected = {
            row["id"]: row
            for rows in self.rows.values()
            for row in rows
            if row["id"] in article_ids
        }
        fields = (
            "title", "publish_time", "source_url", "markdown_path",
            "image_dir", "status",
        )
        with (output / "index.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for article_id in article_ids:
                row = selected[article_id]
                filename = f"article-{article_id}.md"
                (output / filename).write_text(
                    f"# {row['title']}\n\n" + "这是用于离线端到端验证的公开文章正文。" * 12,
                    encoding="utf-8",
                )
                writer.writerow(
                    {
                        "title": row["title"],
                        "publish_time": row["publish_time"],
                        "source_url": row["url"],
                        "markdown_path": filename,
                        "image_dir": "",
                        "status": "success",
                    }
                )
        count = len(article_ids)
        return {
            "ok": True,
            "output_dir": str(output),
            "index": str(output / "index.csv"),
            "selected_count": count,
            "success_count": count,
            "failure_count": 0,
            "skipped_count": 0,
            "failed": [],
            "skipped": [],
        }


class TenProjectDeliveryTests(unittest.TestCase):
    def test_real_pipeline_builds_repeatable_lint_clean_delivery(self) -> None:
        projects = load_projects(REPOSITORY / "config/projects.json")
        self.assertEqual(len(projects), 10)
        backend = OfflineExporter(projects)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=runtime,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )

            first = pipeline.run(projects, since="2026-01-01")
            vault = runtime / "vault/英诺被投项目资讯库"
            article_files = sorted((vault / "03-文章").rglob("*.md"))

            self.assertEqual(first.project_count, 10)
            self.assertEqual(first.article_count, 10)
            self.assertEqual(first.duplicate_count, 1)
            self.assertEqual(len(article_files), 10)
            self.assertFalse(any("2025" in path.name for path in article_files))
            self.assertEqual(len(list((vault / "02-项目").glob("*.md"))), 10)
            self.assertTrue((vault / "01-采集状态.md").is_file())
            self.assertEqual(lint_vault(vault)["errors"], [])

            first_download_count = len(backend.download_calls)
            second = pipeline.run(projects, since="2026-01-01")
            self.assertEqual(second.article_count, 0)
            self.assertEqual(len(backend.download_calls), first_download_count)

            archive = build_delivery_zip(vault, root / "dist")["zip_path"]
            with zipfile.ZipFile(archive) as package:
                roots = {Path(name).parts[0] for name in package.namelist() if name}
                self.assertEqual(roots, {vault.name})
                package.extractall(root / "received")
            self.assertEqual(
                lint_vault(root / "received" / vault.name)["errors"], []
            )

    def test_cli_has_copy_and_run_defaults_with_environment_overrides(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            collect = build_parser().parse_args(["collect"])
            package = build_parser().parse_args(["package"])
            lint = build_parser().parse_args(["lint"])
        self.assertEqual(collect.projects, Path("config/projects.json"))
        self.assertEqual(collect.since, "2026-01-01")
        self.assertEqual(
            collect.exporter_script,
            Path("../moore-wechat-article-downloader/scripts/wechat_exporter.py"),
        )
        self.assertEqual(
            collect.exporter_runtime,
            Path.home() / ".moore/wechat-article-downloader",
        )
        self.assertEqual(collect.runtime, Path("runtime"))
        self.assertEqual(package.vault, Path("runtime/vault/英诺被投项目资讯库"))
        self.assertEqual(package.dist, Path("dist"))
        self.assertIsNone(package.output)
        self.assertEqual(lint.vault, Path("runtime/vault/英诺被投项目资讯库"))

        with patch.dict(
            "os.environ",
            {"INNO_EXPORTER_SCRIPT": "/custom/exporter.py", "INNO_EXPORTER_RUNTIME": "/custom/runtime"},
            clear=True,
        ):
            overridden = build_parser().parse_args(["collect"])
        self.assertEqual(overridden.exporter_script, Path("/custom/exporter.py"))
        self.assertEqual(overridden.exporter_runtime, Path("/custom/runtime"))
