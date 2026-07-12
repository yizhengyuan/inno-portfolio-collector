from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path

from inno_collector.models import PipelineRunResult, ProjectAccount, ProjectRunResult
from inno_collector.pipeline import CollectionPipeline, PipelineCancelledError
from inno_collector.web.controller import WebController
from inno_collector.web.jobs import (
    JobBusyError,
    JobCancelled,
    JobGoneError,
    JobManager,
    JobOutcome,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class WebJobManagerTests(unittest.TestCase):
    def test_only_one_write_job_runs_and_ids_are_opaque(self) -> None:
        release = threading.Event()
        started = threading.Event()
        manager = JobManager()

        def blocking(context):
            started.set()
            release.wait(timeout=2)
            return {"article_count": 3}

        job_id = manager.submit("collection", blocking)
        self.assertTrue(started.wait(timeout=1))
        self.assertGreaterEqual(len(job_id), 24)
        self.assertNotIn("collection", job_id)
        with self.assertRaises(JobBusyError):
            manager.submit("preflight", lambda context: {})

        release.set()
        snapshot = manager.wait(job_id, timeout=2)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["result"], {"article_count": 3})

    def test_jobs_support_partial_failed_and_cancelled_states(self) -> None:
        manager = JobManager()
        partial_id = manager.submit(
            "collection",
            lambda context: JobOutcome("partial", {"failed_projects": 2}),
        )
        self.assertEqual(manager.wait(partial_id, timeout=2)["status"], "partial")

        failed_id = manager.submit(
            "collection",
            lambda context: (_ for _ in ()).throw(RuntimeError("token=secret /Users/private/x")),
        )
        failed = manager.wait(failed_id, timeout=2)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "任务执行失败。")

        waiting = threading.Event()

        def cancellable(context):
            waiting.set()
            while not context.is_cancelled():
                threading.Event().wait(0.005)
            context.checkpoint()

        cancelled_id = manager.submit("collection", cancellable)
        self.assertTrue(waiting.wait(timeout=1))
        manager.cancel(cancelled_id)
        self.assertEqual(manager.wait(cancelled_id, timeout=2)["status"], "cancelled")

    def test_events_are_bounded_allowlisted_and_sanitized(self) -> None:
        manager = JobManager()

        def operation(context):
            context.emit(
                "project_started",
                project="雷鸟创新 /Users/private token=secret",
                stage="catalog",
                counts={"article_count": 2, "bad": -1},
            )
            return {"output_path": "/Users/private/result", "article_count": 2}

        job_id = manager.submit("collection", operation)
        snapshot = manager.wait(job_id, timeout=2)
        event_page = manager.events(job_id)

        self.assertEqual(len(event_page["events"]), 1)
        serialized = repr({"snapshot": snapshot, "events": event_page})
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("output_path", snapshot["result"])
        self.assertEqual(event_page["events"][0]["counts"], {"article_count": 2})

    def test_unknown_or_previous_process_job_is_gone(self) -> None:
        first = JobManager()
        job_id = first.submit("preflight", lambda context: {})
        first.wait(job_id, timeout=2)

        second = JobManager()
        with self.assertRaises(JobGoneError):
            second.get(job_id)

    def test_completed_jobs_have_count_and_age_cleanup_limits(self) -> None:
        clock = ManualClock()
        manager = JobManager(max_completed=2, max_age_seconds=10, clock=clock)
        ids = []
        for _ in range(3):
            job_id = manager.submit("preflight", lambda context: {})
            manager.wait(job_id, timeout=2)
            ids.append(job_id)

        with self.assertRaises(JobGoneError):
            manager.get(ids[0])
        self.assertEqual(manager.get(ids[-1])["status"], "succeeded")

        clock.value += 11
        with self.assertRaises(JobGoneError):
            manager.get(ids[-1])

    def test_pipeline_emits_safe_boundaries_and_honors_cancellation(self) -> None:
        class Backend:
            def auth_check(self):
                return {"ok": True, "status": "valid"}

            def accounts(self):
                return [{"id": 1, "nickname": "Alpha"}]

            def resolve_exact(self, project, rows):
                return rows[0]

            def sync(self, account_id, limit=1000):
                return {"ok": True}

            def articles(self, account_id, limit=5000):
                return []

        class Writer:
            def apply(self, articles, results):
                return None

        project = ProjectAccount(project="Alpha", account="Alpha")
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary:
            pipeline = CollectionPipeline(
                Backend(),
                runtime_dir=Path(temporary) / "runtime",
                vault_writer=Writer(),
            )
            result = pipeline.run(
                [project],
                since="2026-01-01",
                progress=events.append,
            )
            self.assertEqual(result.failed_projects, 0)
            self.assertEqual(
                [event["type"] for event in events],
                [
                    "project_started",
                    "catalog_synced",
                    "articles_selected",
                    "project_finished",
                    "validation_finished",
                ],
            )

            with self.assertRaises(PipelineCancelledError):
                pipeline.run(
                    [project],
                    since="2026-01-01",
                    cancelled=lambda: True,
                )

    def test_controller_requires_current_preflight_then_exposes_collection_job(self) -> None:
        source = Path(__file__).parents[1] / "config/projects.json"
        with tempfile.TemporaryDirectory() as temporary:
            projects_path = Path(temporary) / "projects.json"
            projects_path.write_bytes(source.read_bytes())

            def preflight(projects, since):
                return PipelineRunResult(
                    projects=tuple(
                        ProjectRunResult(
                            project=item.project,
                            account=item.account,
                            discovered=0,
                            downloaded=0,
                            skipped=0,
                            failed=0,
                            status="success",
                            error="",
                        )
                        for item in projects
                    ),
                    project_count=len(projects),
                    failed_projects=0,
                    article_count=0,
                    duplicate_count=0,
                )

            def collect(projects, since, progress, cancelled):
                progress(
                    {
                        "type": "project_started",
                        "project": projects[0].project,
                        "stage": "sync",
                        "counts": {},
                    }
                )
                return PipelineRunResult(
                    projects=(),
                    project_count=10,
                    failed_projects=2,
                    article_count=12,
                    duplicate_count=1,
                )

            controller = WebController(
                Path(temporary) / "vault",
                projects_path=projects_path,
                preflight_runner=preflight,
                collection_runner=collect,
            )

            status, payload = controller("POST", "/api/collection", {})
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], "preflight_required")

            self.assertEqual(controller("POST", "/api/preflight", {})[0], 200)
            status, submitted = controller("POST", "/api/collection", {})
            self.assertEqual(status, 202)
            job_id = submitted["job_id"]
            completed = controller.job_manager.wait(job_id, timeout=2)
            self.assertEqual(completed["status"], "partial")

            status, snapshot = controller("GET", f"/api/jobs/{job_id}", None)
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["result"]["article_count"], 12)
            status, events = controller("GET", f"/api/jobs/{job_id}/events", None)
            self.assertEqual(status, 200)
            self.assertEqual(events["events"][0]["type"], "project_started")

            projects_path.write_bytes(projects_path.read_bytes() + b" ")
            status, payload = controller("POST", "/api/collection", {})
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], "preflight_required")

    def test_controller_job_cancel_and_gone_responses_are_stable(self) -> None:
        source = Path(__file__).parents[1] / "config/projects.json"
        with tempfile.TemporaryDirectory() as temporary:
            projects_path = Path(temporary) / "projects.json"
            projects_path.write_bytes(source.read_bytes())
            started = threading.Event()

            def preflight(projects, since):
                return PipelineRunResult(
                    projects=tuple(
                        ProjectRunResult(
                            item.project,
                            item.account,
                            0,
                            0,
                            0,
                            0,
                            "success",
                            "",
                        )
                        for item in projects
                    ),
                    project_count=10,
                    failed_projects=0,
                    article_count=0,
                    duplicate_count=0,
                )

            def collect(projects, since, progress, cancelled):
                started.set()
                while not cancelled():
                    threading.Event().wait(0.005)
                raise PipelineCancelledError

            controller = WebController(
                Path(temporary) / "vault",
                projects_path=projects_path,
                preflight_runner=preflight,
                collection_runner=collect,
            )
            controller("POST", "/api/preflight", {})
            _, submitted = controller("POST", "/api/collection", {})
            job_id = submitted["job_id"]
            self.assertTrue(started.wait(timeout=1))

            status, _ = controller("POST", f"/api/jobs/{job_id}/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(
                controller.job_manager.wait(job_id, timeout=2)["status"],
                "cancelled",
            )

            status, payload = controller("GET", f"/api/jobs/{'z' * 32}", None)
            self.assertEqual(status, 410)
            self.assertEqual(payload["error"]["code"], "job_gone")

    def test_frontend_exposes_collection_status_and_cancel_routes(self) -> None:
        javascript = (
            Path(__file__).parents[1]
            / "src/inno_collector/web/assets/app.js"
        ).read_text(encoding="utf-8")

        self.assertIn('writeJson("/api/collection"', javascript)
        self.assertIn("/api/jobs/${jobId}", javascript)
        self.assertIn("/cancel", javascript)


if __name__ == "__main__":
    unittest.main()
