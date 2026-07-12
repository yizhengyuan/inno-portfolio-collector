from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from inno_collector.config import load_projects
from inno_collector.exporter import resolve_exact_account
from inno_collector.models import PipelineRunResult, ProjectRunResult
from inno_collector.pipeline import PipelineAuthenticationError
from inno_collector.web.controller import WebController


ROOT = Path(__file__).parents[1]
SOURCE_PROJECTS = ROOT / "config" / "projects.json"
PACKAGED_PROJECTS = (
    ROOT / "src/inno_collector/web/resources/projects.json"
)


def successful_result(projects, since: str) -> PipelineRunResult:
    return PipelineRunResult(
        projects=tuple(
            ProjectRunResult(
                project=project.project,
                account=project.account,
                discovered=index,
                downloaded=0,
                skipped=index,
                failed=0,
                status="success",
                error="",
            )
            for index, project in enumerate(projects, start=1)
        ),
        project_count=len(projects),
        failed_projects=0,
        article_count=0,
        duplicate_count=0,
    )


class WebPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"

    def test_packaged_mapping_is_byte_identical_to_original_config(self) -> None:
        self.assertEqual(PACKAGED_PROJECTS.read_bytes(), SOURCE_PROJECTS.read_bytes())

    def test_preflight_returns_ten_explicit_project_rows_without_mutating_config(self) -> None:
        before = hashlib.sha256(PACKAGED_PROJECTS.read_bytes()).digest()
        controller = WebController(
            self.vault,
            projects_path=PACKAGED_PROJECTS,
            preflight_runner=successful_result,
        )

        status, payload = controller(
            "POST", "/api/preflight", {"since": "2026-01-01"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["projects"]), 10)
        self.assertEqual(
            set(payload["projects"][0]),
            {
                "project",
                "account",
                "mapping",
                "login",
                "catalog",
                "date_filter",
                "status",
                "reason",
            },
        )
        self.assertTrue(all(row["date_filter"] == "2026-01-01" for row in payload["projects"]))
        self.assertEqual(before, hashlib.sha256(PACKAGED_PROJECTS.read_bytes()).digest())

    def test_auth_failure_still_returns_all_ten_rows(self) -> None:
        def fail(projects, since: str):
            raise PipelineAuthenticationError("token=secret /Users/private/login")

        controller = WebController(
            self.vault,
            projects_path=PACKAGED_PROJECTS,
            preflight_runner=fail,
        )

        status, payload = controller("POST", "/api/preflight", {})

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["projects"]), 10)
        self.assertTrue(all(row["login"] == "invalid" for row in payload["projects"]))
        self.assertTrue(all(row["status"] == "failed" for row in payload["projects"]))
        self.assertNotIn("secret", repr(payload))
        self.assertNotIn("/Users/", repr(payload))

    def test_date_filter_is_fixed_to_2026_scope(self) -> None:
        controller = WebController(
            self.vault,
            projects_path=PACKAGED_PROJECTS,
            preflight_runner=successful_result,
        )

        status, payload = controller(
            "POST", "/api/preflight", {"since": "2025-01-01"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_date_filter")

    def test_default_preflight_path_reuses_collection_pipeline_dry_run(self) -> None:
        projects = load_projects(PACKAGED_PROJECTS)

        class CachedBackend:
            def auth_check(self) -> dict:
                return {"ok": True, "status": "valid"}

            def accounts(self) -> list[dict]:
                return [
                    {"id": index, "nickname": project.account, "alias": project.wechat_id}
                    for index, project in enumerate(projects, start=1)
                ]

            def resolve_exact(self, project, rows: list[dict]) -> dict:
                return resolve_exact_account(project, rows)

            def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
                return []

        controller = WebController(
            self.vault,
            moore_runtime=CachedBackend(),
            projects_path=PACKAGED_PROJECTS,
            runtime_dir=Path(self.temp.name) / "runtime",
        )

        status, payload = controller("POST", "/api/preflight", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["projects"]), 10)
        self.assertTrue(all(row["mapping"] == "matched" for row in payload["projects"]))

    def test_real_runtime_explicitly_discovers_missing_exact_accounts_before_dry_run(
        self,
    ) -> None:
        projects = load_projects(PACKAGED_PROJECTS)

        class DiscoveringBackend:
            def __init__(self) -> None:
                self.cached: list[dict] = []
                self.discovered: tuple = ()
                self.auth_checks = 0

            def auth_check(self) -> dict:
                self.auth_checks += 1
                return {"ok": True, "status": "valid"}

            def ensure_exact_accounts(self, requested) -> list[dict]:
                self.discovered = tuple(requested)
                self.cached = [
                    {
                        "id": index,
                        "nickname": project.account,
                        "alias": project.wechat_id,
                    }
                    for index, project in enumerate(self.discovered, start=1)
                ]
                return list(self.cached)

            def accounts(self) -> list[dict]:
                return list(self.cached)

            def resolve_exact(self, project, rows: list[dict]) -> dict:
                return resolve_exact_account(project, rows)

            def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
                return []

        backend = DiscoveringBackend()
        controller = WebController(
            self.vault,
            moore_runtime=backend,
            projects_path=PACKAGED_PROJECTS,
            runtime_dir=Path(self.temp.name) / "runtime",
        )

        status, payload = controller("POST", "/api/preflight", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(backend.discovered, projects)
        self.assertEqual(backend.auth_checks, 2)
        self.assertTrue(all(row["mapping"] == "matched" for row in payload["projects"]))

    def test_account_discovery_failure_is_sanitized_for_every_project(self) -> None:
        class FailingDiscoveryBackend:
            def auth_check(self) -> dict:
                return {"ok": True, "status": "valid"}

            def ensure_exact_accounts(self, _projects) -> list[dict]:
                raise RuntimeError("token=secret /Users/private/account-cache.json")

        controller = WebController(
            self.vault,
            moore_runtime=FailingDiscoveryBackend(),
            projects_path=PACKAGED_PROJECTS,
        )

        status, payload = controller("POST", "/api/preflight", {})

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["projects"]), 10)
        self.assertNotIn("secret", repr(payload))
        self.assertNotIn("/Users/", repr(payload))


if __name__ == "__main__":
    unittest.main()
