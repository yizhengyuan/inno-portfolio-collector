from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from inno_collector.web.controller import WebController
from inno_collector.web.downloads import DownloadRegistry
from inno_collector.web.requests import UploadedFile
from inno_collector.web.responses import FileResponse


class WebDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "Runtime/vault/英诺被投项目资讯库"
        self.vault.mkdir(parents=True)
        self.exporter_runtime = self.root / "ExporterRuntime"
        self.exporter_runtime.mkdir()
        self.delivery_root = self.root / "DeliveryTemp"
        self.registry = DownloadRegistry(
            self.delivery_root,
            vault_root=self.vault,
            exporter_runtime_root=self.exporter_runtime,
        )
        self.calls: list[tuple[Path, Path, Path | None, str | None]] = []
        self.customer_calls: list[tuple[Path, Path]] = []

        def builder(vault, output, *, base_package=None, created_at=None):
            self.calls.append((vault, output, base_package, created_at))
            base = base_package.read_bytes() if base_package is not None else b"baseline"
            output.write_bytes(b"update:" + base)
            return {
                "package_path": str(output),
                "kind": "incremental" if base_package is not None else "baseline",
                "base_version": "sha256:" + "1" * 64 if base_package else None,
                "target_version": "sha256:" + "2" * 64,
                "included": ["00-首页.md", "90-系统/manifest.json"],
                "deleted": [],
                "package_sha256": "ignored-by-controller",
            }

        def customer_builder(vault, output):
            self.customer_calls.append((vault, output))
            output.write_bytes(b"customer-zip")
            return {
                "zip_path": output,
                "summary_path": output.with_suffix(".summary.md"),
                "article_count": 225,
                "successful_projects": 2,
                "failed_projects": 8,
                "zip_sha256": "ignored-by-controller",
            }

        self.controller = WebController(
            self.vault,
            delivery_root=self.delivery_root,
            download_registry=self.registry,
            delivery_builder=builder,
            customer_delivery_builder=customer_builder,
        )

    def _completed(self, submitted: dict) -> dict:
        return self.controller.job_manager.wait(submitted["job_id"], timeout=2)

    def test_baseline_job_registers_safe_one_time_download(self) -> None:
        status, submitted = self.controller(
            "POST",
            "/api/delivery",
            {"kind": "baseline", "created_at": "2026-07-12T12:00:00Z"},
        )
        self.assertEqual(status, 202)
        completed = self._completed(submitted)
        self.assertEqual(completed["status"], "succeeded")
        result = completed["result"]
        self.assertEqual(result["kind"], "baseline")
        self.assertEqual(result["included_count"], 2)
        self.assertNotIn("package_path", result)
        self.assertNotIn(str(self.root), repr(result))

        status, response = self.controller(
            "GET", f"/api/delivery/{result['download_id']}/download", None
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.size, len(b"update:baseline"))
        self.assertEqual(response.filename.endswith(".inno-update"), True)
        self.assertEqual(response.content_type, "application/zip")
        self.assertEqual(response.path.read_bytes(), b"update:baseline")
        response.on_complete(True)
        self.assertFalse(response.path.exists())

        status, payload = self.controller(
            "GET", f"/api/delivery/{result['download_id']}/download", None
        )
        self.assertEqual(status, 410)
        self.assertEqual(payload["error"]["code"], "download_gone")

    def test_incremental_upload_is_copied_before_request_temp_is_removed(self) -> None:
        upload_path = self.root / "request-upload.tmp"
        with zipfile.ZipFile(upload_path, "w") as archive:
            archive.writestr("update-manifest.json", b"{}")
        uploaded = UploadedFile(
            filename="previous.inno-update",
            content_type="application/octet-stream",
            path=upload_path,
            size=upload_path.stat().st_size,
        )

        status, submitted = self.controller("POST", "/api/delivery", uploaded)
        self.assertEqual(status, 202)
        upload_path.unlink()
        completed = self._completed(submitted)

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["kind"], "incremental")
        _vault, output, base, _created = self.calls[-1]
        self.assertEqual(output.parent, self.delivery_root)
        self.assertIsNotNone(base)
        self.assertFalse(base.exists())
        self.assertFalse(str(output).startswith(str(self.vault)))
        self.assertFalse(str(output).startswith(str(self.exporter_runtime)))

    def test_customer_package_job_registers_safe_one_time_zip_download(self) -> None:
        status, submitted = self.controller(
            "POST", "/api/delivery", {"kind": "customer"}
        )

        self.assertEqual(status, 202)
        completed = self._completed(submitted)
        self.assertEqual(completed["status"], "succeeded")
        result = completed["result"]
        self.assertEqual(result["kind"], "customer")
        self.assertEqual(result["article_count"], 225)
        self.assertEqual(result["successful_projects"], 2)
        self.assertEqual(result["failed_projects"], 8)
        self.assertTrue(result["filename"].startswith("英诺客户资料库-"))
        self.assertTrue(result["filename"].endswith(".zip"))
        self.assertNotIn(str(self.root), repr(result))
        self.assertEqual(self.customer_calls[0][0], self.vault)

        status, response = self.controller(
            "GET", f"/api/delivery/{result['download_id']}/download", None
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.path.read_bytes(), b"customer-zip")
        response.on_complete(True)
        self.assertFalse(response.path.exists())

    def test_invalid_delivery_parameters_are_rejected_without_paths(self) -> None:
        for payload in (
            {"kind": "incremental"},
            {"kind": "unknown"},
            UploadedFile(
                filename="wrong.zip",
                content_type="application/zip",
                path=self.root / "missing",
                size=0,
            ),
        ):
            with self.subTest(payload=type(payload).__name__):
                status, response = self.controller("POST", "/api/delivery", payload)
                self.assertEqual(status, 400)
                self.assertNotIn(str(self.root), repr(response))

    def test_frontend_presents_single_customer_zip_and_one_time_download(self) -> None:
        javascript = (
            Path(__file__).parents[1] / "src/inno_collector/web/assets/app.js"
        ).read_text(encoding="utf-8")
        html = (
            Path(__file__).parents[1] / "src/inno_collector/web/assets/index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('writeJson("/api/delivery"', javascript)
        self.assertIn("/api/delivery/${job.result.download_id}/download", javascript)
        self.assertIn("生成客户资料包 ZIP", html)
        self.assertIn('submitDelivery({ kind: "customer" })', javascript)
        self.assertNotIn('accept=".inno-update"', html)


if __name__ == "__main__":
    unittest.main()
