from __future__ import annotations

import unittest
import json
import os
import subprocess
import tempfile
from pathlib import Path

from inno_collector.package import lint_vault
from scripts.build_helpers import audit_reader_binary


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_HELPER = os.environ.get("INNO_COLLECTOR_HELPER")
READER_HELPER = os.environ.get("INNO_READER_HELPER")


class DistributionLicenseTests(unittest.TestCase):
    def test_required_mit_notices_are_vendored_verbatim(self) -> None:
        exporter = (
            ROOT / "third_party/licenses/wechat-article-exporter-LICENSE.txt"
        ).read_text(encoding="utf-8")
        moore = (
            ROOT / "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("Copyright (c) 2024 Jock", exporter)
        self.assertIn("Copyright (c) 2026 Moore-developers", moore)
        self.assertIn("Permission is hereby granted", exporter)
        self.assertIn("Permission is hereby granted", moore)
        self.assertTrue(exporter.endswith("\n"))
        self.assertTrue(moore.endswith("\n"))


class DistributionDocumentationTests(unittest.TestCase):
    def test_readme_and_manual_gate_cover_two_user_distribution(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        checklist = (ROOT / "docs/macos-release-checklist.md").read_text(encoding="utf-8")
        for phrase in (
            "英诺资讯采集", "英诺资讯阅读", "Obsidian", "离线看板",
            "公众号登录凭据", "文章版权",
        ):
            self.assertIn(phrase, readme)
        for phrase in (
            "macOS 13", "Gatekeeper", "Python 和 Codex", "断开网络",
            "稿件字节", "codesign", "spctl", "第三方许可证",
        ):
            self.assertIn(phrase, checklist)


@unittest.skipUnless(
    COLLECTOR_HELPER and READER_HELPER,
    "requires INNO_COLLECTOR_HELPER and INNO_READER_HELPER",
)
class FrozenTwoUserWorkflowTests(unittest.TestCase):
    def call(
        self,
        helper: Path,
        command: str,
        arguments: dict[str, object],
        environment: dict[str, str],
    ) -> dict[str, object]:
        request = {"id": f"e2e-{command}", "command": command, "arguments": arguments}
        result = subprocess.run(
            [str(helper)],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=environment,
            timeout=180,
            check=False,
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"{command} returned invalid helper output")
        self.assertTrue(response.get("ok"), response.get("error"))
        self.assertEqual(response.get("id"), request["id"])
        payload = response.get("result")
        self.assertIsInstance(payload, dict)
        return payload

    def test_frozen_collector_reader_round_trip_preserves_human_work(self) -> None:
        collector_helper = Path(str(COLLECTOR_HELPER)).resolve()
        reader_helper = Path(str(READER_HELPER)).resolve()
        fixture = ROOT / "tests/fixtures/offline_exporter.py"
        self.assertTrue(fixture.is_file())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collector_home = root / "collector-home"
            reader_home = root / "reader-home"
            collector_home.mkdir()
            reader_home.mkdir()
            collector_environment = {
                **os.environ,
                "HOME": str(collector_home),
                "INNO_OFFLINE_PROJECTS": str(ROOT / "config/projects.json"),
                "INNO_OFFLINE_REVISION": "1",
            }
            reader_environment = {**os.environ, "HOME": str(reader_home)}
            collector_runtime = collector_home / "Library/Application Support/collector/Runtime"
            exporter_runtime = collector_home / "Library/Application Support/collector/ExporterRuntime"
            collector_vault = collector_runtime / "vault/英诺被投项目资讯库"
            reader_vault = reader_home / "Library/Application Support/reader/英诺被投项目资讯库"

            collected = self.call(
                collector_helper,
                "collect",
                {
                    "projects": str(ROOT / "config/projects.json"),
                    "runtime": str(collector_runtime),
                    "exporter_runtime": str(exporter_runtime),
                    "exporter_executable": str(fixture),
                    "since": "2026-01-01",
                    "dry_run": False,
                },
                collector_environment,
            )
            self.assertEqual(collected["project_count"], 10)
            baseline = root / "baseline.inno-update"
            self.call(
                collector_helper,
                "build_update",
                {
                    "vault": str(collector_vault),
                    "output": str(baseline),
                    "created_at": "2026-07-11T12:00:00Z",
                },
                collector_environment,
            )
            preview = self.call(
                reader_helper,
                "preview_update",
                {"package": str(baseline)},
                reader_environment,
            )
            self.assertEqual(preview["kind"], "baseline")
            self.call(
                reader_helper,
                "apply_update",
                {"package": str(baseline), "vault": str(reader_vault)},
                reader_environment,
            )
            manifest = json.loads(
                (reader_vault / "90-系统/manifest.json").read_text(encoding="utf-8")
            )
            source_id = next(iter(manifest["articles"]))
            draft_result = self.call(
                reader_helper,
                "create_draft",
                {
                    "vault": str(reader_vault),
                    "draft_id": "frozen-round-trip",
                    "draft_version": 1,
                    "author": "朋友甲",
                    "title": "冻结端往返稿件",
                    "updated_at": "2026-07-11T13:00:00+08:00",
                    "source_ids": [source_id],
                    "kind": "edit",
                    "body": "人工内容不可覆盖。",
                },
                reader_environment,
            )
            draft = Path(str(draft_result["draft_path"]))
            original_draft = draft.read_bytes()

            collector_environment["INNO_OFFLINE_REVISION"] = "2"
            self.call(
                collector_helper,
                "collect",
                {
                    "projects": str(ROOT / "config/projects.json"),
                    "runtime": str(collector_runtime),
                    "exporter_runtime": str(exporter_runtime),
                    "exporter_executable": str(fixture),
                    "since": "2026-01-01",
                    "dry_run": False,
                },
                collector_environment,
            )
            incremental = root / "incremental.inno-update"
            self.call(
                collector_helper,
                "build_update",
                {
                    "vault": str(collector_vault),
                    "output": str(incremental),
                    "base_package": str(baseline),
                    "created_at": "2026-07-11T13:30:00Z",
                },
                collector_environment,
            )
            preview = self.call(
                reader_helper,
                "preview_update",
                {"package": str(incremental)},
                reader_environment,
            )
            self.assertEqual(preview["kind"], "incremental")
            self.call(
                reader_helper,
                "apply_update",
                {"package": str(incremental), "vault": str(reader_vault)},
                reader_environment,
            )
            self.assertEqual(draft.read_bytes(), original_draft)

            draft_package = root / "friend.inno-drafts"
            self.call(
                reader_helper,
                "build_drafts",
                {
                    "vault": str(reader_vault),
                    "draft_paths": ["10-编辑稿/frozen-round-trip.md"],
                    "output": str(draft_package),
                    "exported_at": "2026-07-11T14:00:00+08:00",
                },
                reader_environment,
            )
            receipt = self.call(
                collector_helper,
                "receive_drafts",
                {"package": str(draft_package), "inbox": str(collector_home / "DraftInbox")},
                collector_environment,
            )
            self.assertEqual(receipt["draft_count"], 1)
            restored = self.call(
                collector_helper,
                "list_received_drafts",
                {"inbox": str(collector_home / "DraftInbox")},
                collector_environment,
            )
            self.assertEqual(
                restored["receipts"],
                [{
                    "receipt_path": receipt["receipt_path"],
                    "draft_count": 1,
                }],
            )
            accepted = self.call(
                collector_helper,
                "accept_draft",
                {
                    "receipt": str(receipt["receipt_path"]),
                    "vault": str(collector_vault),
                },
                collector_environment,
            )
            self.assertEqual(accepted["draft_count"], 1)
            self.assertTrue(
                (collector_vault / "10-编辑稿/frozen-round-trip.md").is_file()
            )
            self.assertEqual(lint_vault(reader_vault)["errors"], [])
            audit_reader_binary(reader_helper)
            reader_root = self.bundle_root(reader_helper)
            forbidden = {"projects.json", "InnoCollectorHelper", "MooreExporterHelper"}
            self.assertFalse(any(path.name in forbidden for path in reader_root.rglob("*")))
            for scan_root in (collector_home, reader_home, reader_root):
                for path in scan_root.rglob("*"):
                    if path.is_file() and path.suffix.casefold() in {".json", ".md", ".txt"}:
                        self.assertNotIn("fixture-secret", path.read_text(encoding="utf-8"))

    @staticmethod
    def bundle_root(helper: Path) -> Path:
        plugins = helper.parent
        if plugins.name == "PlugIns" and plugins.parent.name == "Contents":
            return plugins.parent.parent
        return plugins


if __name__ == "__main__":
    unittest.main()
