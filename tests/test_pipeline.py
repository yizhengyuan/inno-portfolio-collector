from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
    PipelineDeliveryError,
    catalog_fingerprint,
)
from inno_collector.state import CatalogStateStore


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
        self.fail_on_calls: set[int] = set()

    def apply(self, articles: list[NormalizedArticle], results: list[object]) -> None:
        self.calls.append((articles.copy(), results.copy()))
        if len(self.calls) in self.fail_on_calls:
            raise OSError("vault write failed /Users/private token=vault-secret")


class FakeBackend:
    def __init__(self, projects: list[ProjectAccount]) -> None:
        self.projects = projects
        self.calls: list[tuple] = []
        self.rows = {
            index: [article_row(index * 10, f"slug-{index}")]
            for index in range(1, len(projects) + 1)
        }
        self.fail_sync_ids: set[int] = set()
        self.sync_errors: dict[int, str] = {}
        self.download_errors: dict[int, str] = {}
        self.accounts_error: str = ""
        self.auth_payload: object = {"ok": True, "status": "valid"}

    def auth_check(self) -> object:
        self.calls.append(("auth_check",))
        return self.auth_payload

    def accounts(self) -> list[dict]:
        self.calls.append(("accounts",))
        if self.accounts_error:
            raise RuntimeError(self.accounts_error)
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
        if account_id in self.sync_errors:
            raise RuntimeError(self.sync_errors[account_id])
        return {"ok": True}

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
        self.calls.append(("articles", account_id, limit))
        return self.rows[account_id]

    def download(self, article_ids: list[int], output_root: Path) -> dict:
        self.calls.append(("download", tuple(article_ids), output_root))
        if article_ids and article_ids[0] in self.download_errors:
            raise RuntimeError(self.download_errors[article_ids[0]])
        output_dir = output_root / "account"
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "output_dir": str(output_dir),
            "index": str(output_dir / "index.csv"),
            "selected_count": len(article_ids),
            "success_count": len(article_ids),
            "failure_count": 0,
            "skipped_count": 0,
            "skipped": [],
            "failed": [],
        }


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

    def test_resolves_first_then_applies_each_account_and_final_report(self) -> None:
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
                "resolve_exact",
                "sync",
                "articles",
                "download",
                "sync",
                "articles",
                "download",
            ],
        )
        self.assertEqual(len(vault.calls), 3)
        self.assertEqual([len(call[0]) for call in vault.calls], [1, 1, 0])
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
        self.assertEqual(result.projects[1].last_sync, "")
        self.assertEqual(len(vault.calls), 3)

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
        self.assertEqual(result.projects[0].last_sync, "")
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

    def test_accounts_retries_only_transient_failures_before_any_artifact(self) -> None:
        cases = (("HTTP 503 temporary unavailable", 3, [1.0, 3.0]), ("403 forbidden", 1, []))
        for message, expected_calls, expected_delays in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_dir:
                project = self.project(1)
                backend = FakeBackend([project])
                backend.accounts_error = message
                delays: list[float] = []
                runtime = Path(temp_dir) / "runtime"
                pipeline = CollectionPipeline(
                    backend,
                    runtime_dir=runtime,
                    vault_writer=RecordingVault(),
                    now=lambda: NOW,
                    sleep=delays.append,
                )

                with self.assertRaises(PipelineConfigurationError):
                    pipeline.run([project], since="2026-01-01")

                self.assertEqual(
                    sum(call[0] == "accounts" for call in backend.calls),
                    expected_calls,
                )
                self.assertEqual(delays, expected_delays)
                self.assertFalse(runtime.exists())

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
            runtime = Path(temp_dir) / "runtime"
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=runtime,
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

    def test_manifest_without_catalog_state_is_refetched_once_then_skipped(self) -> None:
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

            first = pipeline.run([project], since="2026-01-01")
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "articles": {
                            existing_key: {
                                "key": existing_key,
                                "content_hash": "sha256:" + "a" * 64,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            backend.calls.clear()
            second = self.build_pipeline(backend, runtime, vault).run(
                [project], since="2026-01-01"
            )

        self.assertEqual(first.projects[0].downloaded, 1)
        self.assertFalse(any(call[0] == "download" for call in backend.calls))
        self.assertEqual(second.projects[0].skipped, 1)
        self.assertEqual(second.projects[0].downloaded, 0)

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
            "catalog: 3 invalid or duplicate ids",
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
            runtime = Path(temp_dir) / "runtime"
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=runtime,
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
            "ingest: rejected 1 requested article; "
            "ingest: output omitted 1 requested article",
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
                PipelineConfigurationError,
                "^catalog: existing manifest is invalid$",
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
                return {
                    "ok": True,
                    "output_dir": str(outside),
                    "index": str(outside / "index.csv"),
                    "selected_count": 1,
                    "success_count": 1,
                    "failure_count": 0,
                    "skipped_count": 0,
                    "skipped": [],
                    "failed": [],
                }

            backend.download = unsafe_download  # type: ignore[method-assign]
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)
            result = pipeline.run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].status, "failed")
        self.assertEqual(result.article_count, 0)
        self.assertEqual(vault.calls[0][0], [])
        self.assertEqual(
            result.projects[0].error,
            "download: exporter returned unsafe output directory",
        )

    def test_malformed_download_payload_is_rejected_even_with_safe_path(self) -> None:
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
        self.assertEqual(
            result.projects[0].error,
            "download: exporter returned invalid download response",
        )

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
        self.assertTrue(all(item.last_sync == "" for item in result.projects))

    def test_partial_download_ingests_successes_and_counts_failures_once(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(10, "good-one"),
            article_row(11, "good-two"),
            article_row(12, "bad"),
        ]
        vault = RecordingVault()

        def partial_download(article_ids: list[int], output_root: Path) -> dict:
            backend.calls.append(("download", tuple(article_ids), output_root))
            output_dir = output_root / "account"
            output_dir.mkdir(parents=True)
            return {
                "ok": False,
                "output_dir": str(output_dir),
                "index": str(output_dir / "index.csv"),
                "selected_count": 3,
                "success_count": 2,
                "failure_count": 1,
                "skipped_count": 0,
                "skipped": [],
                "failed": [{"article_id": 12}],
            }

        backend.download = partial_download  # type: ignore[method-assign]

        def ingest(_project: ProjectAccount, _root: Path) -> IngestResult:
            return IngestResult(
                valid=(
                    normalized(project, "good-one"),
                    normalized(project, "good-two"),
                ),
                rejected=(
                    RejectedArticle(
                        "坏文章",
                        "https://mp.weixin.qq.com/s/bad",
                        "download_failed",
                    ),
                ),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=runtime,
                vault_writer=vault,
                ingest=ingest,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            result = pipeline.run([project], since="2026-01-01")
            state = CatalogStateStore(runtime / "state" / "catalog-state.json")
            self.assertIsNotNone(
                state.get(article_key("https://mp.weixin.qq.com/s/good-one"))
            )
            self.assertIsNotNone(
                state.get(article_key("https://mp.weixin.qq.com/s/good-two"))
            )
            self.assertIsNone(
                state.get(article_key("https://mp.weixin.qq.com/s/bad"))
            )

        self.assertEqual(result.projects[0].downloaded, 2)
        self.assertEqual(result.projects[0].failed, 1)
        self.assertEqual(result.projects[0].status, "partial")
        self.assertEqual(len(vault.calls[0][0]), 2)
        self.assertEqual(vault.calls[-1][0], [])

    def test_exporter_failed_key_cannot_be_forged_as_success_in_index(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        row = backend.rows[1][0]
        vault = RecordingVault()

        def partial_download(article_ids: list[int], output_root: Path) -> dict:
            backend.calls.append(("download", tuple(article_ids), output_root))
            output = output_root / "account"
            output.mkdir()
            return {
                "ok": False,
                "output_dir": str(output),
                "index": str(output / "index.csv"),
                "selected_count": 1,
                "success_count": 0,
                "failure_count": 1,
                "skipped_count": 0,
                "skipped": [],
                "failed": [{"source_url": row["url"]}],
            }

        backend.download = partial_download  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=lambda _project, _root: IngestResult(
                    valid=(normalized(project, "slug-1"),), rejected=()
                ),
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            ).run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].downloaded, 0)
        self.assertEqual(result.projects[0].failed, 1)
        self.assertEqual(result.projects[0].status, "failed")
        self.assertEqual([len(call[0]) for call in vault.calls], [0])

    def test_exporter_failed_and_skipped_keys_are_both_excluded_from_ingest(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [article_row(10, "failed"), article_row(11, "skipped")]
        vault = RecordingVault()

        def partial_download(article_ids: list[int], output_root: Path) -> dict:
            backend.calls.append(("download", tuple(article_ids), output_root))
            output = output_root / "account"
            output.mkdir()
            return {
                "ok": False,
                "output_dir": str(output),
                "index": str(output / "index.csv"),
                "selected_count": 2,
                "success_count": 0,
                "failure_count": 1,
                "skipped_count": 1,
                "failed": [
                    {"source_url": "https://mp.weixin.qq.com/s/failed"}
                ],
                "skipped": [
                    {
                        "article_id": 11,
                        "source_url": "https://mp.weixin.qq.com/s/skipped",
                    }
                ],
            }

        backend.download = partial_download  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=lambda _project, _root: IngestResult(
                    valid=(
                        normalized(project, "failed"),
                        normalized(project, "skipped"),
                    ),
                    rejected=(),
                ),
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            ).run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].downloaded, 0)
        self.assertEqual(result.projects[0].failed, 1)
        self.assertEqual(result.projects[0].skipped, 1)
        self.assertEqual([len(call[0]) for call in vault.calls], [0])

    def test_partial_payload_rejects_unknown_duplicate_or_mismatched_entries(self) -> None:
        cases = (
            (
                "unknown",
                [{"source_url": "https://mp.weixin.qq.com/s/unknown"}],
                [],
            ),
            (
                "duplicate",
                [
                    {"source_url": "https://mp.weixin.qq.com/s/one"},
                    {"source_url": "https://mp.weixin.qq.com/s/one"},
                ],
                [],
            ),
            (
                "mismatch",
                [
                    {
                        "article_id": 10,
                        "source_url": "https://mp.weixin.qq.com/s/two",
                    }
                ],
                [],
            ),
            (
                "unknown-skipped",
                [{"source_url": "https://mp.weixin.qq.com/s/one"}],
                [{"source_url": "https://mp.weixin.qq.com/s/unknown"}],
            ),
        )
        for name, failed_rows, skipped_rows in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                project = self.project(1)
                backend = FakeBackend([project])
                backend.rows[1] = [article_row(10, "one"), article_row(11, "two")]
                vault = RecordingVault()

                def malformed_download(
                    article_ids: list[int], output_root: Path
                ) -> dict:
                    backend.calls.append(("download", tuple(article_ids), output_root))
                    output = output_root / "account"
                    output.mkdir()
                    failure_count = len(failed_rows)
                    skipped_count = len(skipped_rows)
                    return {
                        "ok": False,
                        "output_dir": str(output),
                        "index": str(output / "index.csv"),
                        "selected_count": 2,
                        "success_count": 2 - failure_count - skipped_count,
                        "failure_count": failure_count,
                        "skipped_count": skipped_count,
                        "skipped": skipped_rows,
                        "failed": failed_rows,
                    }

                backend.download = malformed_download  # type: ignore[method-assign]
                result = CollectionPipeline(
                    backend,
                    runtime_dir=Path(temp_dir) / "runtime",
                    vault_writer=vault,
                    ingest=lambda _project, _root: IngestResult(
                        valid=(normalized(project, "one"),), rejected=()
                    ),
                    now=lambda: NOW,
                    sleep=lambda _seconds: None,
                ).run([project], since="2026-01-01")

                self.assertEqual(result.projects[0].downloaded, 0)
                self.assertEqual(result.projects[0].status, "failed")
                self.assertTrue(result.projects[0].error.startswith("download:"))
                self.assertEqual([len(call[0]) for call in vault.calls], [0])

    def test_ingest_and_state_exceptions_include_stable_stage_names(self) -> None:
        for stage in ("ingest", "state"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp_dir:
                project = self.project(1)
                backend = FakeBackend([project])
                vault = RecordingVault()
                ingest = (
                    (lambda _project, _root: (_ for _ in ()).throw(
                        RuntimeError("temporary failure token=stage-secret")
                    ))
                    if stage == "ingest"
                    else (
                        lambda _project, _root: IngestResult(
                            valid=(normalized(project, "slug-1"),),
                            rejected=(),
                        )
                    )
                )
                pipeline = CollectionPipeline(
                    backend,
                    runtime_dir=Path(temp_dir) / "runtime",
                    vault_writer=vault,
                    ingest=ingest,
                    now=lambda: NOW,
                    sleep=lambda _seconds: None,
                )
                save_patch = (
                    patch(
                        "inno_collector.pipeline.CatalogStateStore.save",
                        side_effect=OSError("state /Users/yzy/private token=state-secret"),
                    )
                    if stage == "state"
                    else patch("inno_collector.pipeline.CatalogStateStore.save", autospec=True)
                )
                if stage == "ingest":
                    result = pipeline.run([project], since="2026-01-01")
                else:
                    with save_patch:
                        result = pipeline.run([project], since="2026-01-01")

                self.assertTrue(result.projects[0].error.startswith(f"{stage}:"))
                self.assertNotIn("stage-secret", result.projects[0].error)
                self.assertNotIn("state-secret", result.projects[0].error)
                self.assertNotIn("/Users/", result.projects[0].error)

    def test_vault_failure_is_isolated_and_does_not_advance_catalog_state(self) -> None:
        projects = [self.project(1), self.project(2)]
        backend = FakeBackend(projects)
        vault = RecordingVault()
        vault.fail_on_calls.add(1)
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            pipeline = self.build_pipeline(backend, runtime, vault)

            result = pipeline.run(projects, since="2026-01-01")

            state = CatalogStateStore(runtime / "state" / "catalog-state.json")
            first_key = article_key("https://mp.weixin.qq.com/s/slug-1")
            second_key = article_key("https://mp.weixin.qq.com/s/slug-2")
            self.assertIsNone(state.get(first_key))
            self.assertIsNotNone(state.get(second_key))

        self.assertEqual([item.status for item in result.projects], ["failed", "success"])
        self.assertEqual([item.downloaded for item in result.projects], [0, 1])
        self.assertGreaterEqual(result.projects[0].failed, 1)
        self.assertNotIn("vault-secret", result.projects[0].error)
        self.assertTrue(result.projects[0].error.startswith("vault:"))
        self.assertEqual([len(call[0]) for call in vault.calls], [1, 1, 0])

    def test_cleanup_failure_is_local_redacted_and_preserves_successful_vault_write(self) -> None:
        projects = [self.project(1), self.project(2)]
        backend = FakeBackend(projects)
        vault = RecordingVault()

        class CleanupFailingPipeline(CollectionPipeline):
            cleanup_calls = 0

            def _cleanup_output_root(self, run_root: Path) -> None:
                self.cleanup_calls += 1
                if self.cleanup_calls == 1:
                    raise OSError(
                        "cleanup /Users/yzy/private token=cleanup-secret"
                    )
                super()._cleanup_output_root(run_root)

        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = CleanupFailingPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=lambda project, _root: IngestResult(
                    valid=(
                        normalized(
                            project,
                            f"slug-{backend.projects.index(project) + 1}",
                        ),
                    ),
                    rejected=(),
                ),
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )

            result = pipeline.run(projects, since="2026-01-01")

        self.assertEqual([item.status for item in result.projects], ["partial", "success"])
        self.assertEqual([item.downloaded for item in result.projects], [1, 1])
        self.assertNotIn("cleanup-secret", result.projects[0].error)
        self.assertNotIn("/Users/", result.projects[0].error)
        self.assertIn("cleanup:", result.projects[0].error)
        self.assertEqual([len(call[0]) for call in vault.calls], [1, 1, 0])

    def test_final_report_failure_raises_stable_delivery_error(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        vault = RecordingVault()
        vault.fail_on_calls.add(2)
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = self.build_pipeline(backend, Path(temp_dir) / "runtime", vault)

            with self.assertRaisesRegex(
                PipelineDeliveryError,
                "^report: failed to rebuild collection report$",
            ):
                pipeline.run([project], since="2026-01-01")

    def test_non_transient_errors_are_not_retried_and_batch_failure_is_not_underreported(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(10, "one"),
            article_row(11, "two"),
            article_row(12, "three"),
        ]
        backend.download_errors[10] = "403 forbidden"
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend, Path(temp_dir) / "runtime", vault
            ).run([project], since="2026-01-01")

        downloads = [call for call in backend.calls if call[0] == "download"]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(result.projects[0].failed, 3)
        self.assertEqual(result.projects[0].status, "failed")

    def test_transient_and_non_transient_sync_errors_have_distinct_retry_counts(self) -> None:
        cases = (
            ("temporary unavailable", 3),
            ("HTTP 503 invalid upstream response", 3),
            ("HTTP 503 authentication failed", 1),
            ("503 permission denied", 1),
            ("503 invalid format", 1),
            ("403 forbidden", 1),
            ("invalid format", 1),
        )
        for message, expected_calls in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_dir:
                project = self.project(1)
                backend = FakeBackend([project])
                backend.sync_errors[1] = message
                vault = RecordingVault()
                result = self.build_pipeline(
                    backend, Path(temp_dir) / "runtime", vault
                ).run([project], since="2026-01-01")

                sync_calls = [call for call in backend.calls if call[0] == "sync"]
                self.assertEqual(len(sync_calls), expected_calls)
                self.assertEqual(result.projects[0].last_sync, "")
                self.assertTrue(result.projects[0].error.startswith("sync:"))

    def test_sync_payload_must_be_successful_and_is_not_retried(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        vault = RecordingVault()

        def unsuccessful_sync(account_id: int, limit: int = 1000) -> dict:
            backend.calls.append(("sync", account_id, limit))
            return {"ok": False}

        backend.sync = unsuccessful_sync  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend, Path(temp_dir) / "runtime", vault
            ).run([project], since="2026-01-01")

        self.assertEqual(sum(call[0] == "sync" for call in backend.calls), 1)
        self.assertEqual(result.projects[0].last_sync, "")
        self.assertEqual(result.projects[0].status, "failed")

    def test_all_accounts_are_resolved_before_any_sync_and_match_failure_is_local(self) -> None:
        projects = [self.project(1), self.project(2), self.project(3)]
        backend = FakeBackend(projects)
        original_resolve = backend.resolve_exact

        def resolve(project: ProjectAccount, rows: list[dict]) -> dict:
            if project.project == "项目2":
                backend.calls.append(("resolve_exact", project.project))
                raise RuntimeError("expected one exact account match")
            return original_resolve(project, rows)

        backend.resolve_exact = resolve  # type: ignore[method-assign]
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend, Path(temp_dir) / "runtime", vault
            ).run(projects, since="2026-01-01")

        names = [call[0] for call in backend.calls]
        self.assertEqual(names[:5], ["auth_check", "accounts", "resolve_exact", "resolve_exact", "resolve_exact"])
        self.assertEqual([item.status for item in result.projects], ["success", "failed", "success"])
        self.assertEqual(result.projects[1].last_sync, "")
        self.assertTrue(result.projects[1].error.startswith("resolve:"))

    def test_catalog_fingerprint_skips_silent_rows_but_refetches_metadata_changes(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        row = backend.rows[1][0]
        row.update(
            {
                "title": "标题一",
                "digest": "摘要一",
                "author": "作者",
                "updated_at": "volatile-one",
                "raw_json": json.dumps(
                    {"stable": "one", "updated_at": "volatile-one"}
                ),
            }
        )
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            manifest = runtime / "vault" / "英诺被投项目资讯库" / "90-系统" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            key = article_key(row["url"])
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "articles": {
                            key: {
                                "key": key,
                                "content_hash": "sha256:" + "a" * 64,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.build_pipeline(backend, runtime, vault).run(
                [project], since="2026-01-01"
            )
            backend.calls.clear()
            row["updated_at"] = "volatile-two"
            row["raw_json"] = json.dumps(
                {"stable": "one", "updated_at": "volatile-two"}
            )
            silent = self.build_pipeline(backend, runtime, vault).run(
                [project], since="2026-01-01"
            )
            self.assertFalse(any(call[0] == "download" for call in backend.calls))
            self.assertEqual(silent.projects[0].skipped, 1)

            backend.calls.clear()
            row["title"] = "标题二"
            title_changed = self.build_pipeline(backend, runtime, vault).run(
                [project], since="2026-01-01"
            )
            self.assertTrue(any(call[0] == "download" for call in backend.calls))
            self.assertEqual(title_changed.projects[0].downloaded, 1)

            backend.calls.clear()
            row["digest"] = "摘要二"
            changed = self.build_pipeline(backend, runtime, vault).run(
                [project], since="2026-01-01"
            )
            self.assertTrue(any(call[0] == "download" for call in backend.calls))
            self.assertEqual(changed.projects[0].downloaded, 1)

            backend.calls.clear()
            row["raw_json"] = json.dumps(
                {"stable": "two", "updated_at": "volatile-three"}
            )
            raw_changed = self.build_pipeline(backend, runtime, vault).run(
                [project], since="2026-01-01"
            )

        self.assertTrue(any(call[0] == "download" for call in backend.calls))
        self.assertEqual(raw_changed.projects[0].downloaded, 1)

    def test_content_verification_is_bounded_and_refreshes_silent_body_changes(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        row = backend.rows[1][0]
        old_hash = "sha256:" + "1" * 64
        new_hash = "sha256:" + "2" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            manifest = (
                runtime
                / "vault"
                / "英诺被投项目资讯库"
                / "90-系统"
                / "manifest.json"
            )
            manifest.parent.mkdir(parents=True)
            key = article_key(row["url"])
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "articles": {
                            key: {"key": key, "content_hash": old_hash}
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_path = runtime / "state" / "catalog-state.json"
            state = CatalogStateStore(state_path)
            state.mark_success(
                key,
                catalog_fingerprint(row),
                content_hash=old_hash,
                verified_at=NOW.isoformat(),
            )
            state.save()

            within_backend = FakeBackend([project])
            within_backend.rows[1] = [dict(row)]
            within = CollectionPipeline(
                within_backend,
                runtime_dir=runtime,
                vault_writer=RecordingVault(),
                ingest=lambda _project, _root: IngestResult(valid=(), rejected=()),
                now=lambda: NOW + timedelta(hours=23),
                sleep=lambda _seconds: None,
                verification_interval=timedelta(hours=24),
            ).run([project], since="2026-01-01")
            self.assertFalse(
                any(call[0] == "download" for call in within_backend.calls)
            )
            self.assertEqual(within.projects[0].skipped, 1)

            expired_backend = FakeBackend([project])
            expired_backend.rows[1] = [dict(row)]
            updated_article = replace(
                normalized(project, "slug-1"),
                content_hash=new_hash,
                body="静默变化后的正文" * 20,
            )
            expired = CollectionPipeline(
                expired_backend,
                runtime_dir=runtime,
                vault_writer=RecordingVault(),
                ingest=lambda _project, _root: IngestResult(
                    valid=(updated_article,), rejected=()
                ),
                now=lambda: NOW + timedelta(hours=25),
                sleep=lambda _seconds: None,
                verification_interval=timedelta(hours=24),
            ).run([project], since="2026-01-01")

            refreshed = CatalogStateStore(state_path).get_record(key)

        self.assertTrue(any(call[0] == "download" for call in expired_backend.calls))
        self.assertEqual(expired.projects[0].downloaded, 1)
        assert refreshed is not None
        self.assertEqual(refreshed["content_hash"], new_hash)
        self.assertEqual(
            refreshed["verified_at"],
            (NOW + timedelta(hours=25)).isoformat(),
        )

    def test_legacy_catalog_state_and_manifest_hash_mismatch_force_refresh(self) -> None:
        for mode in ("legacy", "hash-mismatch"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                project = self.project(1)
                backend = FakeBackend([project])
                row = backend.rows[1][0]
                runtime = Path(temp_dir) / "runtime"
                manifest = (
                    runtime
                    / "vault"
                    / "英诺被投项目资讯库"
                    / "90-系统"
                    / "manifest.json"
                )
                manifest.parent.mkdir(parents=True)
                key = article_key(row["url"])
                manifest_hash = "sha256:" + "1" * 64
                manifest.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "articles": {
                                key: {"key": key, "content_hash": manifest_hash}
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                state = CatalogStateStore(runtime / "state" / "catalog-state.json")
                if mode == "legacy":
                    state.mark_success(key, catalog_fingerprint(row))
                else:
                    state.mark_success(
                        key,
                        catalog_fingerprint(row),
                        content_hash="sha256:" + "2" * 64,
                        verified_at=NOW.isoformat(),
                    )
                state.save()

                result = self.build_pipeline(
                    backend, runtime, RecordingVault()
                ).run([project], since="2026-01-01")

                self.assertTrue(
                    any(call[0] == "download" for call in backend.calls)
                )
                self.assertEqual(result.projects[0].downloaded, 1)

    def test_expired_verification_failure_does_not_advance_state(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        row = backend.rows[1][0]
        key = article_key(row["url"])
        old_hash = "sha256:" + "1" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            manifest = (
                runtime
                / "vault"
                / "英诺被投项目资讯库"
                / "90-系统"
                / "manifest.json"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "articles": {
                            key: {"key": key, "content_hash": old_hash}
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_path = runtime / "state" / "catalog-state.json"
            state = CatalogStateStore(state_path)
            state.mark_success(
                key,
                catalog_fingerprint(row),
                content_hash=old_hash,
                verified_at=NOW.isoformat(),
            )
            state.save()
            before = state_path.read_bytes()
            vault = RecordingVault()
            vault.fail_on_calls.add(1)

            CollectionPipeline(
                backend,
                runtime_dir=runtime,
                vault_writer=vault,
                ingest=lambda _project, _root: IngestResult(
                    valid=(normalized(project, "slug-1"),), rejected=()
                ),
                now=lambda: NOW + timedelta(hours=25),
                sleep=lambda _seconds: None,
                verification_interval=timedelta(hours=24),
            ).run([project], since="2026-01-01")

            self.assertEqual(state_path.read_bytes(), before)

    def test_invalid_or_missing_urls_after_cutoff_are_discovered_failures(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(10, "valid"),
            {"id": 11, "url": "", "publish_time": "2026-01-02"},
            {
                "id": 12,
                "url": "https://example.com/not-wechat",
                "publish_time": "2026-01-03",
            },
            {
                "id": 13,
                "url": "https://example.com/old",
                "publish_time": "2025-12-31",
            },
            {"id": 14, "url": "", "publish_time": "not-a-date"},
        ]
        vault = RecordingVault()
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=Path(temp_dir) / "runtime",
                vault_writer=vault,
                ingest=lambda _project, _root: IngestResult(
                    valid=(normalized(project, "valid"),), rejected=()
                ),
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            result = pipeline.run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].discovered, 3)
        self.assertEqual(result.projects[0].downloaded, 1)
        self.assertEqual(result.projects[0].failed, 2)
        self.assertEqual(result.projects[0].status, "partial")
        self.assertIn("catalog: 2 invalid or missing urls", result.projects[0].error)

    def test_catalog_failures_and_batch_exception_are_counted_additively(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        backend.rows[1] = [
            article_row(10, "valid"),
            {"id": 11, "url": "", "publish_time": "2026-01-02"},
            {
                "id": 12,
                "url": "https://example.com/not-wechat",
                "publish_time": "2026-01-03",
            },
        ]
        backend.download_errors[10] = "403 forbidden"
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend,
                Path(temp_dir) / "runtime",
                RecordingVault(),
            ).run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].discovered, 3)
        self.assertEqual(result.projects[0].failed, 3)
        self.assertEqual(result.projects[0].status, "failed")
        self.assertIn("2 invalid or missing urls", result.projects[0].error)
        self.assertIn("403 forbidden", result.projects[0].error)

    def test_failed_vault_write_does_not_make_next_run_skip_changed_catalog(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            manifest = runtime / "vault" / "英诺被投项目资讯库" / "90-系统" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            key = article_key(backend.rows[1][0]["url"])
            manifest.write_text(
                json.dumps({"version": 1, "articles": {key: {"key": key}}}),
                encoding="utf-8",
            )
            self.build_pipeline(backend, runtime, RecordingVault()).run(
                [project], since="2026-01-01"
            )
            state_path = runtime / "state" / "catalog-state.json"
            fingerprint_before_failure = CatalogStateStore(state_path).get(key)
            backend.rows[1][0]["digest"] = "changed-after-success"
            backend.calls.clear()
            failing_vault = RecordingVault()
            failing_vault.fail_on_calls.add(1)
            self.build_pipeline(backend, runtime, failing_vault).run(
                [project], since="2026-01-01"
            )
            self.assertEqual(
                CatalogStateStore(state_path).get(key),
                fingerprint_before_failure,
            )
            backend.calls.clear()

            self.build_pipeline(backend, runtime, RecordingVault()).run(
                [project], since="2026-01-01"
            )

        self.assertTrue(any(call[0] == "download" for call in backend.calls))

    def test_secure_staging_rejects_symlinked_runtime_staging_or_account_directory(self) -> None:
        for bad_level in ("runtime", "staging", "account"):
            with self.subTest(level=bad_level), tempfile.TemporaryDirectory() as temp_dir:
                base = Path(temp_dir)
                outside = base / "outside"
                outside.mkdir()
                runtime = base / "runtime"
                if bad_level == "runtime":
                    runtime.symlink_to(outside, target_is_directory=True)
                else:
                    runtime.mkdir()
                    staging = runtime / "staging"
                    if bad_level == "staging":
                        staging.symlink_to(outside, target_is_directory=True)
                    else:
                        staging.mkdir()
                        (staging / "01-1").symlink_to(
                            outside, target_is_directory=True
                        )
                project = self.project(1)
                backend = FakeBackend([project])
                pipeline = self.build_pipeline(backend, runtime, RecordingVault())
                if bad_level in {"runtime", "staging"}:
                    with self.assertRaisesRegex(
                        PipelineConfigurationError,
                        "^staging: unsafe runtime staging directory$",
                    ):
                        pipeline.run([project], since="2026-01-01")
                else:
                    result = pipeline.run([project], since="2026-01-01")
                    self.assertEqual(result.projects[0].status, "failed")
                    self.assertTrue(
                        result.projects[0].error.startswith("staging:")
                    )
                self.assertFalse(any(call[0] == "sync" for call in backend.calls))
                self.assertFalse(any(call[0] == "download" for call in backend.calls))

    def test_each_download_uses_a_fresh_staging_directory_and_cleans_it(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend, Path(temp_dir) / "runtime", RecordingVault()
            ).run([project], since="2026-01-01")
            output_root = next(
                call[2] for call in backend.calls if call[0] == "download"
            )

            self.assertFalse(output_root.exists())
        self.assertEqual(result.projects[0].status, "success")

    def test_download_output_directory_itself_must_not_be_a_symlink(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])

        def symlinked_download(article_ids: list[int], output_root: Path) -> dict:
            backend.calls.append(("download", tuple(article_ids), output_root))
            actual = output_root / "actual"
            actual.mkdir()
            output = output_root / "account"
            output.symlink_to(actual, target_is_directory=True)
            return {
                "ok": True,
                "output_dir": str(output),
                "index": str(output / "index.csv"),
                "selected_count": 1,
                "success_count": 1,
                "failure_count": 0,
                "skipped_count": 0,
                "skipped": [],
                "failed": [],
            }

        backend.download = symlinked_download  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend, Path(temp_dir) / "runtime", RecordingVault()
            ).run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].status, "failed")
        self.assertEqual(
            result.projects[0].error,
            "download: exporter returned unsafe output directory",
        )

    def test_download_index_must_be_the_safe_index_inside_output_directory(self) -> None:
        project = self.project(1)
        backend = FakeBackend([project])

        def wrong_index_download(article_ids: list[int], output_root: Path) -> dict:
            backend.calls.append(("download", tuple(article_ids), output_root))
            output = output_root / "account"
            output.mkdir()
            return {
                "ok": True,
                "output_dir": str(output),
                "index": str(output / "other.csv"),
                "selected_count": 1,
                "success_count": 1,
                "failure_count": 0,
                "skipped_count": 0,
                "skipped": [],
                "failed": [],
            }

        backend.download = wrong_index_download  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.build_pipeline(
                backend, Path(temp_dir) / "runtime", RecordingVault()
            ).run([project], since="2026-01-01")

        self.assertEqual(result.projects[0].status, "failed")
        self.assertEqual(
            result.projects[0].error,
            "download: exporter returned unsafe output directory",
        )


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
        self.assertEqual(stderr.getvalue(), "collection failed: bad config\n")

    @patch(
        "inno_collector.cli.load_projects",
        side_effect=FileNotFoundError(
            2,
            "missing token=config-secret",
            "/Users/yzy/My Project/projects.json",
        ),
    )
    def test_collect_error_output_never_leaks_local_paths_or_secrets(
        self, _load: object
    ) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            exit_code = main(
                [
                    "collect",
                    "--projects",
                    "/Users/yzy/My Project/projects.json",
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

        output = stderr.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("[path]", output)
        self.assertNotIn("/Users/", output)
        self.assertNotIn("yzy", output)
        self.assertNotIn("config-secret", output)

    @patch(
        "inno_collector.cli.load_projects",
        side_effect=TypeError(
            "unexpected '/Volumes/Private Disk/projects.json' token=type-secret"
        ),
    )
    def test_collect_unexpected_exception_uses_same_sanitized_boundary(
        self, _load: object
    ) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
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

        self.assertEqual(exit_code, 2)
        self.assertIn("[path]", stderr.getvalue())
        self.assertNotIn("Private Disk", stderr.getvalue())
        self.assertNotIn("type-secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
