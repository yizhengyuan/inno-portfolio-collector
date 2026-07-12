from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inno_collector.config import load_projects
from inno_collector.pipeline import CollectionPipeline
from inno_collector.reader_helper import _apply_update, _build_drafts, _preview_update
from inno_collector.web.controller import WebController
from inno_collector.web.downloads import DownloadRegistry
from inno_collector.web.requests import UploadedFile
from inno_collector.web.responses import FileResponse
from inno_collector.web.uploads import DraftUploadManager
from tests.test_end_to_end import NOW, OfflineExporter


ROOT = Path(__file__).parents[1]


class WebRoundTripTests(unittest.TestCase):
    def _download(self, controller: WebController, result: dict, destination: Path) -> None:
        status, response = controller(
            "GET", f"/api/delivery/{result['download_id']}/download", None
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(response, FileResponse)
        destination.write_bytes(response.path.read_bytes())
        response.on_complete(True)

    def test_collector_web_reader_and_draft_round_trip(self) -> None:
        projects = load_projects(ROOT / "config/projects.json")
        backend = OfflineExporter(projects)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "Runtime"
            pipeline = CollectionPipeline(
                backend,
                runtime_dir=runtime,
                now=lambda: NOW,
                sleep=lambda _seconds: None,
            )
            pipeline.run(projects, since="2026-01-01")
            collector_vault = runtime / "vault/英诺被投项目资讯库"
            exporter_runtime = root / "ExporterRuntime"
            exporter_runtime.mkdir()
            delivery_root = root / "DeliveryTemp"
            registry = DownloadRegistry(
                delivery_root,
                vault_root=collector_vault,
                exporter_runtime_root=exporter_runtime,
            )
            controller = WebController(
                collector_vault,
                delivery_root=delivery_root,
                download_registry=registry,
                draft_upload_manager=DraftUploadManager(root / "DraftInbox"),
            )

            status, submitted = controller(
                "POST",
                "/api/delivery",
                {"kind": "baseline", "created_at": "2026-07-12T12:00:00Z"},
            )
            self.assertEqual(status, 202)
            baseline_job = controller.job_manager.wait(submitted["job_id"], timeout=10)
            self.assertEqual(baseline_job["status"], "succeeded")
            baseline = root / "baseline.inno-update"
            self._download(controller, baseline_job["result"], baseline)

            preview = _preview_update({"package": str(baseline)})
            self.assertEqual(preview["kind"], "baseline")
            reader_vault = root / "ReaderVault"
            _apply_update({"package": str(baseline), "vault": str(reader_vault)})
            manifest = json.loads(
                (reader_vault / "90-系统/manifest.json").read_text(encoding="utf-8")
            )
            source_id = next(iter(manifest["articles"]))
            reader_draft = reader_vault / "10-编辑稿/web-round-trip.md"
            reader_draft.write_text(
                "---\n"
                'draft_id: "web-round-trip"\n'
                "draft_version: 1\n"
                'author: "朋友甲"\n'
                'title: "网页往返稿件"\n'
                'updated_at: "2026-07-12T13:00:00+08:00"\n'
                f'source_ids: ["{source_id}"]\n'
                "---\n\n人工内容不可覆盖。\n",
                encoding="utf-8",
            )
            original_draft = reader_draft.read_bytes()

            backend.rows[1].append(
                {
                    "id": 999,
                    "url": "https://mp.weixin.qq.com/s/web-incremental",
                    "publish_time": "2026-07-12 10:00:00",
                    "title": "网页增量资讯",
                }
            )
            pipeline.run(projects, since="2026-01-01")
            uploaded_base = UploadedFile(
                filename="baseline.inno-update",
                content_type="application/octet-stream",
                path=baseline,
                size=baseline.stat().st_size,
            )
            status, submitted = controller("POST", "/api/delivery", uploaded_base)
            self.assertEqual(status, 202)
            incremental_job = controller.job_manager.wait(submitted["job_id"], timeout=10)
            self.assertEqual(incremental_job["status"], "succeeded")
            incremental = root / "incremental.inno-update"
            self._download(controller, incremental_job["result"], incremental)
            self.assertEqual(_preview_update({"package": str(incremental)})["kind"], "incremental")
            _apply_update({"package": str(incremental), "vault": str(reader_vault)})
            self.assertEqual(reader_draft.read_bytes(), original_draft)

            draft_package = root / "friend.inno-drafts"
            _build_drafts(
                {
                    "vault": str(reader_vault),
                    "draft_paths": ["10-编辑稿/web-round-trip.md"],
                    "output": str(draft_package),
                    "exported_at": "2026-07-12T14:00:00+08:00",
                }
            )
            uploaded_draft = UploadedFile(
                filename="friend.inno-drafts",
                content_type="application/octet-stream",
                path=draft_package,
                size=draft_package.stat().st_size,
            )
            status, draft_preview = controller(
                "POST", "/api/drafts/preview", uploaded_draft
            )
            self.assertEqual(status, 200)
            self.assertNotIn(str(root), repr(draft_preview))
            status, accepted = controller(
                "POST",
                f"/api/drafts/{draft_preview['receipt_id']}/accept",
                {"confirm": True},
            )
            self.assertEqual(status, 200)
            self.assertEqual(accepted["created"], 1)
            collected_drafts = list((collector_vault / "10-编辑稿").glob("*.md"))
            self.assertEqual(len(collected_drafts), 1)
            self.assertIn("人工内容不可覆盖", collected_drafts[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
