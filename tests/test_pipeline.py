from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from inno_collector.cli import build_parser, main
from inno_collector.identity import article_key
from inno_collector.models import (
    IngestResult,
    NormalizedArticle,
    PipelineRunResult,
    ProjectAccount,
    ProjectRunResult,
    RejectedArticle,
)
from inno_collector.pipeline import (
    CollectionPipeline,
    PipelineAuthenticationError,
    PipelineConfigurationError,
)


NOW = datetime(2026, 7, 11, 9, 30, tzinfo=timezone.utc)


def article_row(
    article_id: object,
    slug: str,
    published: str = "2026-01-01 08:00:00",
) -> dict:
    return {
        "id": article_id,
        "url": f"https://mp.weixin.qq.com/s/{slug}",
        "publish_time": published,
    }


def normalized(project: ProjectAccount, slug: str) -> NormalizedArticle:
    source_url = f"https://mp.weixin.qq.com/s/{slug}"
    return NormalizedArticle(
        key=article_key(source_url),
        project=project.project,
        account=project.account,
        title=f"文章-{slug}",
        published="2026-01-01",
        source_url=source_url,
        collected_at=NOW.isoformat(),
        content_hash="sha256:" + "a" * 64,
        body="正文" * 50,
        source_markdown=Path("article.md"),
    )


class RecordingVault:
    def __init__(self) -> None:
        self.calls: list[tuple[list[NormalizedArticle], list[object]]] = []

    def apply(self, articles: list[NormalizedArticle], results: list[object]) -> None:
        self.calls.append((articles.copy(), results.copy()))


class FakeBackend:
    def __init__(self, projects: list[ProjectAccount]) -> None:
        self.projects = projects
        self.calls: list[tuple] = []
        self.rows = {
            index: [article_row(index * 10, f"slug-{index}")]
            for index in range(1, len(projects) + 1)
        }
        self.fail_sync_ids: set[int] = set()
        self.auth_payload: object = {"ok": True, "status": "valid"}

    def auth_check(self) -> object:
        self.calls.append(("auth_check",))
        return self.auth_payload

    def accounts(self) -> list[dict]:
        self.calls.append(("accounts",))
        return [
            {"id": index, "nickname": project.account, "alias": project.wechat_id}
            for index, project in enumerate(self.projects, start=1)
        ]

    def resolve_exact(self, project: ProjectAccount, rows: list[dict]) -> dict:
        self.calls.append(("resolve_exact", project.project))
        return next(row for row in rows if row["nickname"] == project.account)

    def sync(self, account_id: int, limit: int = 1000) -> dict:
        self.calls.append(("sync", account_id, limit))
        if account_id in self.fail_sync_ids:
            raise RuntimeError("temporary failure token=do-not-leak")
        return {"ok": True}

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
        self.calls.append(("articles", account_id, limit))
        return self.rows[account_id]

    def download(self, article_ids: list[int], output_root: Path) -> dict:
        self.calls.append(("download", tuple(article_ids), output_root))
        output_dir = output_root / "account"
        output_dir.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "output_dir": str(output_dir)}


class CollectionPipelineTests(unittest.TestCase):
    def project(self, index: int) -> ProjectAccount:
        return ProjectAccount(f"项目{index}", f"账号{index}")

    def build_pipeline(
        self,
        backend: FakeBackend,
        runtime: Path,
        vault: RecordingVault,
    ) -> CollectionPipeline:
        def ingest(project: ProjectAccount, _root: Path) -> IngestResult:
            index = backend.projects.index(project) + 1
            return IngestResult(
                valid=(normalized(project, f"slug-{index}"),),
                rejected=(),
            )

        return CollectionPipeline(
            backend,
            runtime_dir=runtime,
            vault_writer=vault,
            ingest=ingest,
            now=lambda: NOW,
            sleep=lambda _seconds: None,
        )

    def test_calls_backend_in_fixed_order_and_applies_vault_once(self) -> None:
        projects = [self.project(1), self.project(2)]
        backend = FakeBackend(projects)
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)

            result = pipeline.run(projects, since="2026-01-01")

        call_names = [call[0] for call in backend.calls]
        self.assertEqual(
            call_names,
            [
                "auth_check",
                "accounts",
                "resolve_exact",
                "sync",
                "articles",
                "download",
                "resolve_exact",
                "sync",
                "articles",
                "download",
            ],
        )
        self.assertEqual(len(vault.calls), 1)
        self.assertEqual([item.project for item in result.projects], ["项目1", "项目2"])
        self.assertEqual([item.last_sync for item in result.projects], [NOW.isoformat()] * 2)
        self.assertEqual(result.article_count, 2)

    def test_one_account_failure_is_retried_and_does_not_stop_next_account(self) -> None:
        projects = [self.project(1), self.project(2), self.project(3)]
        backend = FakeBackend(projects)
        backend.fail_sync_ids.add(2)
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)

            result = pipeline.run(projects, since="2026-01-01")

        self.assertEqual([item.status for item in result.projects], ["success", "failed", "success"])
        self.assertEqual(result.failed_projects, 1)
        self.assertEqual(sum(call[:2] == ("sync", 2) for call in backend.calls), 3)
        self.assertIn(("sync", 3, 1000), backend.calls)
        self.assertNotIn("do-not-leak", result.projects[1].error)
        self.assertEqual(len(vault.calls), 1)

    def test_dry_run_reads_cached_articles_but_never_syncs_downloads_or_writes(self) -> None:
        projects = [self.project(1)]
        backend = FakeBackend(projects)
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "does-not-exist"
            pipeline = self.build_pipeline(backend, runtime, vault)

            result = pipeline.run(projects, since="2026-01-01", dry_run=True)

            self.assertFalse(runtime.exists())
        self.assertEqual(
            [call[0] for call in backend.calls],
            ["auth_check", "accounts", "resolve_exact", "articles"],
        )
        self.assertEqual(result.projects[0].discovered, 1)
        self.assertEqual(result.projects[0].downloaded, 0)
        self.assertEqual(vault.calls, [])

    def test_authentication_requires_exact_valid_status_and_creates_no_artifacts(self) -> None:
        for payload in (
            {"ok": True},
            {"ok": True, "status": "VALID"},
            {"ok": "true", "status": "valid"},
            {"ok": True, "status": "expired"},
            None,
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp_dir:
                projects = [self.project(1)]
                backend = FakeBackend(projects)
                backend.auth_payload = payload
                runtime = Path(temp_dir) / "runtime"
                vault = RecordingVault()
                pipeline = self.build_pipeline(backend, runtime, vault)

                with self.assertRaisesRegex(
                    PipelineAuthenticationError,
                    "^exporter authentication is not valid$",
                ):
                    pipeline.run(projects, since="2026-01-01")

                self.assertFalse(runtime.exists())
                self.assertEqual(vault.calls, [])
                self.assertEqual(backend.calls, [("auth_check",)])

    def test_since_must_be_an_exact_iso_date_before_any_backend_call(self) -> None:
        projects = [self.project(1)]
        backend = FakeBackend(projects)
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)

            with self.assertRaisesRegex(
                PipelineConfigurationError, "^since must be an ISO date$"
            ):
                pipeline.run(projects, since="2026-01-01T00:00:00")

        self.assertEqual(backend.calls, [])
        self.assertEqual(vault.calls, [])

    def test_cutoff_is_inclusive_and_empty_selection_never_downloads(self) -> None:
        projects = [self.project(1), self.project(2)]
        backend = FakeBackend(projects)
        backend.rows[1] = [
            article_row(11, "before", "2025-12-31 23:59:59"),
            article_row(12, "inclusive", "2026-01-01 00:00:00"),
        ]
        backend.rows[2] = [article_row(21, "old", "2025-12-30")]
        vault = RecordingVault()

        def ingest(project: ProjectAccount, _root: Path) -> IngestResult:
            return IngestResult(valid=(normalized(project, "inclusive"),), rejected=())

        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=ingest,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            result = pipeline.run(projects, since="2026-01-01")

        downloads = [call for call in backend.calls if call[0] == "download"]
        self.assertEqual(downloads[0][1], (12,))
        self.assertEqual(len(downloads), 1)
        self.assertEqual([item.discovered for item in result.projects], [1, 0])
        self.assertEqual([item.status for item in result.projects], ["success", "success"])

    def test_manifest_keys_are_skipped_without_download(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        vault = RecordingVault()
        existing_url = backend.rows[1][0]["url"]
        existing_key = article_key(existing_url)
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            manifest = runtime / "vault" / "英诺被投项目资讯库" / "90-系统" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {"version": 1, "articles": {existing_key: {"key": existing_key}}}
                ),
                encoding="utf-8",
            )
            pipeline = self.build_pipeline(backend, runtime, vault)

            result = pipeline.run([project], since="2026-01-01")

        self.assertFalse(any(call[0] == "download" for call in backend.calls))
        self.assertEqual(result.projects[0].skipped, 1)
        self.assertEqual(result.projects[0].downloaded, 0)
        self.assertEqual(len(vault.calls), 1)

    def test_invalid_and_duplicate_ids_are_not_passed_to_exporter(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(7, "valid-one"),
            article_row(7, "duplicate-id"),
            article_row(True, "bool-id"),
            article_row("8", "string-id"),
        ]
        vault = RecordingVault()

        def ingest(_project: ProjectAccount, _root: Path) -> IngestResult:
            return IngestResult(valid=(normalized(project, "valid-one"),), rejected=())

        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=ingest,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            result = pipeline.run([project], since="2026-01-01")

        download = next(call for call in backend.calls if call[0] == "download")
        self.assertEqual(download[1], (7,))
        self.assertEqual(result.projects[0].discovered, 4)
        self.assertEqual(result.projects[0].downloaded, 1)
        self.assertEqual(result.projects[0].failed, 3)
        self.assertEqual(result.projects[0].status, "partial")
        self.assertEqual(
            result.projects[0].error,
            "article catalog contained 3 invalid or duplicate ids",
        )

    def test_empty_or_non_numeric_ids_never_trigger_download(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(None, "none"),
            article_row(0, "zero"),
            article_row("", "empty"),
        ]
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)
            result = pipeline.run([project], since="2026-01-01")

        self.assertFalse(any(call[0] == "download" for call in backend.calls))
        self.assertEqual(result.projects[0].failed, 3)
        self.assertEqual(result.projects[0].status, "failed")

    def test_ingest_only_accepts_articles_requested_in_this_download(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [article_row(10, "requested")]
        vault = RecordingVault()

        def ingest(_project: ProjectAccount, _root: Path) -> IngestResult:
            return IngestResult(
                valid=(normalized(project, "stale"), normalized(project, "requested")),
                rejected=(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=ingest,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            result = pipeline.run([project], since="2026-01-01")

        self.assertEqual(result.article_count, 1)
        self.assertEqual(vault.calls[0][0][0].source_url, "https://mp.weixin.qq.com/s/requested")

    def test_ingest_rejections_and_missing_requested_rows_record_stable_stages(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(10, "rejected"),
            article_row(11, "missing"),
        ]
        vault = RecordingVault()

        def ingest(_project: ProjectAccount, _root: Path) -> IngestResult:
            return IngestResult(
                valid=(),
                rejected=(
                    RejectedArticle(
                        "坏文章",
                        "https://mp.weixin.qq.com/s/rejected",
                        "invalid_body",
                    ),
                ),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=ingest,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            result = pipeline.run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].failed, 2)
        self.assertEqual(
            result.projects[0].error,
            "ingest rejected 1 requested article; "
            "download output omitted 1 requested article",
        )

    def test_pipeline_rejects_non_strict_existing_manifest_before_writes(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            manifest = runtime / "vault" / "英诺被投项目资讯库" / "90-系统" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"version": True, "articles": {"key": "not-an-object"}}),
                encoding="utf-8",
            )
            pipeline = self.build_pipeline(backend, runtime, vault)

            with self.assertRaisesRegex(
                PipelineConfigurationError, "^existing manifest is invalid$"
            ):
                pipeline.run([project], since="2026-01-01")

        self.assertEqual(vault.calls, [])

    def test_output_directory_must_resolve_within_account_staging_root(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            outside = Path(temp_dir) / "outside"
            outside.mkdir()

            def unsafe_download(article_ids: list[int], output_root: Path) -> dict:
                backend.calls.append(("download", tuple(article_ids), output_root))
                return {"ok": True, "output_dir": str(outside)}

            backend.download = unsafe_download  # type: ignore[method-assign]
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)
            result = pipeline.run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].status, "failed")
        self.assertEqual(result.article_count, 0)
        self.assertEqual(vault.calls[0][0], [])
        self.assertEqual(result.projects[0].error, "exporter returned unsafe output directory")

    def test_unsuccessful_download_payload_is_rejected_even_with_safe_path(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        vault = RecordingVault()

        def unsuccessful_download(article_ids: list[int], output_root: Path) -> dict:
            backend.calls.append(("download", tuple(article_ids), output_root))
            output_dir = output_root / "account"
            output_dir.mkdir(parents=True)
            return {"ok": False, "output_dir": str(output_dir)}

        backend.download = unsuccessful_download  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)
            result = pipeline.run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].status, "failed")
        self.assertEqual(result.projects[0].error, "exporter download was unsuccessful")

    def test_ten_project_results_preserve_configuration_order(self) -> None:
        projects = [self.project(index) for index in range(1, 11)]
        backend = FakeBackend(projects)
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)
            result = pipeline.run(projects, since="2026-01-01", dry_run=True)

        self.assertEqual(
            [item.project for item in result.projects],
            [item.project for item in projects],
        )
        self.assertTrue(all(item.last_sync == NOW.isoformat() for item in result.projects))


class CliTests(unittest.TestCase):
    def test_collect_parser_requires_explicit_paths_and_accepts_dry_run(self) -> None:
        args = build_parser().parse_args(
            [
                "collect",
                "--projects",
                "config/projects.json",
                "--since",
                "2026-01-01",
                "--exporter-script",
                "/repo/exporter.py",
                "--exporter-runtime",
                "/runtime/exporter",
                "--runtime",
                "runtime",
                "--dry-run",
            ]
        )

        self.assertEqual(args.command, "collect")
        self.assertEqual(args.projects, Path("config/projects.json"))
        self.assertEqual(args.since, "2026-01-01")
        self.assertTrue(args.dry_run)

    @patch("inno_collector.cli.CollectionPipeline")
    @patch("inno_collector.cli.MooreExporterAdapter")
    @patch("inno_collector.cli.load_projects")
    def test_collect_command_runs_pipeline_and_uses_stable_exit_codes(
        self,
        load_projects_mock: object,
        adapter_mock: object,
        pipeline_mock: object,
    ) -> None:
        project = ProjectAccount("项目", "账号")
        load_projects_mock.return_value = (project,)  # type: ignore[attr-defined]
        run_result = PipelineRunResult(
            projects=(ProjectRunResult("项目", "账号", 1, 0, 0, 1, "failed", "x", NOW.isoformat()),),
            project_count=1,
            failed_projects=1,
            article_count=0,
            duplicate_count=0,
        )
        pipeline_mock.return_value.run.return_value = run_result  # type: ignore[attr-defined]

        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "collect",
                    "--projects",
                    "projects.json",
                    "--since",
                    "2026-01-01",
                    "--exporter-script",
                    "exporter.py",
                    "--exporter-runtime",
                    "exporter-runtime",
                    "--runtime",
                    "runtime",
                ]
            )

        self.assertEqual(exit_code, 1)
        adapter_mock.assert_called_once_with(Path("exporter.py"), Path("exporter-runtime"))  # type: ignore[attr-defined]
        pipeline_mock.assert_called_once_with(adapter_mock.return_value, runtime_dir=Path("runtime"))  # type: ignore[attr-defined]
        pipeline_mock.return_value.run.assert_called_once_with(  # type: ignore[attr-defined]
            (project,), since="2026-01-01", dry_run=False
        )
        self.assertEqual(json.loads(stdout.getvalue())["failed_projects"], 1)

    @patch("inno_collector.cli.load_projects", side_effect=ValueError("bad config"))
    def test_collect_config_or_auth_failure_returns_two(self, _load: object) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            exit_code = main(
                [
                    "collect",
                    "--projects",
                    "bad.json",
                    "--since",
                    "2026-01-01",
                    "--exporter-script",
                    "exporter.py",
                    "--exporter-runtime",
                    "exporter-runtime",
                    "--runtime",
                    "runtime",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "collection setup failed: bad config\n")


if __name__ == "__main__":
    unittest.main()
