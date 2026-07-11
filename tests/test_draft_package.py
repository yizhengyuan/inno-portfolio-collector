from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from inno_collector.draft_package import (
    DraftPackageError,
    accept_received_draft,
    build_draft_package,
    list_received_drafts,
    receive_draft_package,
)
from inno_collector.vault import VaultWriter


SOURCE_ID = "sha256:" + "1" * 64


class DraftPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.reader_vault = self.root / "reader"
        self.collector_vault = self.root / "collector"
        VaultWriter(self.reader_vault).apply([], [])
        VaultWriter(self.collector_vault).apply([], [])
        self.draft = self.reader_vault / "10-编辑稿/稿件.md"
        attachment = self.reader_vault / "10-编辑稿/附件/draft-one/image.png"
        attachment.parent.mkdir(parents=True)
        attachment.write_bytes(b"png")
        self.write_draft("首版人工稿")

    def write_draft(self, body: str) -> None:
        self.draft.write_text(
            "---\n"
            'draft_id: "draft-one"\n'
            "draft_version: 1\n"
            'author: "朋友甲"\n'
            'title: "项目选题稿"\n'
            'updated_at: "2026-07-11T13:00:00+08:00"\n'
            f'source_ids: ["{SOURCE_ID}"]\n'
            "---\n\n"
            f"{body}\n\n"
            "![](附件/draft-one/image.png)\n",
            encoding="utf-8",
        )

    def build_and_receive(self, name: str) -> Path:
        package = self.root / f"{name}.inno-drafts"
        build_draft_package(
            self.reader_vault,
            ["10-编辑稿/稿件.md"],
            package,
            exported_at="2026-07-11T13:05:00+08:00",
        )
        received = receive_draft_package(package, self.root / "inbox")
        return Path(received["receipt_path"])

    def test_round_trip_exports_only_selected_draft_and_attachment(self) -> None:
        package = self.root / "draft.inno-drafts"

        built = build_draft_package(
            self.reader_vault,
            ["10-编辑稿/稿件.md"],
            package,
            exported_at="2026-07-11T13:05:00+08:00",
        )
        received = receive_draft_package(package, self.root / "inbox")
        accepted = accept_received_draft(
            Path(received["receipt_path"]),
            self.collector_vault,
        )

        self.assertEqual(built["draft_count"], 1)
        self.assertEqual(accepted["created"], 1)
        accepted_drafts = list((self.collector_vault / "10-编辑稿").glob("*.md"))
        self.assertEqual(len(accepted_drafts), 1)
        self.assertIn("首版人工稿", accepted_drafts[0].read_text(encoding="utf-8"))
        self.assertEqual(
            (self.collector_vault / "10-编辑稿/附件/draft-one/image.png").read_bytes(),
            b"png",
        )
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "draft-manifest.json",
                    "payload/10-编辑稿/稿件.md",
                    "payload/10-编辑稿/附件/draft-one/image.png",
                ],
            )

    def test_same_id_and_version_with_different_content_is_kept_as_conflict(self) -> None:
        first = self.build_and_receive("first")
        accept_received_draft(first, self.collector_vault)
        self.write_draft("冲突人工稿")
        second = self.build_and_receive("second")

        result = accept_received_draft(second, self.collector_vault)

        drafts = sorted((self.collector_vault / "10-编辑稿").glob("*.md"))
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(len(drafts), 2)
        self.assertEqual(
            {"首版人工稿", "冲突人工稿"},
            {
                "首版人工稿" if "首版人工稿" in path.read_text(encoding="utf-8") else "冲突人工稿"
                for path in drafts
            },
        )

    def test_same_path_with_different_id_never_overwrites_existing_human_draft(self) -> None:
        existing = self.collector_vault / "10-编辑稿/稿件.md"
        existing.write_text(
            "---\n"
            'draft_id: "draft-two"\n'
            "draft_version: 1\n"
            'author: "采集者"\n'
            'title: "本地人工稿"\n'
            'updated_at: "2026-07-11T12:00:00+08:00"\n'
            f'source_ids: ["{SOURCE_ID}"]\n'
            "---\n\n"
            "本地人工内容不可覆盖。\n",
            encoding="utf-8",
        )
        original = existing.read_bytes()
        receipt = self.build_and_receive("different-id-same-path")

        result = accept_received_draft(receipt, self.collector_vault)

        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["conflicts"], 1)
        conflicts = list((self.collector_vault / "10-编辑稿").glob("稿件-conflict-*.md"))
        self.assertEqual(len(conflicts), 1)
        self.assertIn("首版人工稿", conflicts[0].read_text(encoding="utf-8"))

    def test_identical_receipt_and_accept_are_idempotent(self) -> None:
        package = self.root / "same.inno-drafts"
        build_draft_package(
            self.reader_vault,
            ["10-编辑稿/稿件.md"],
            package,
            exported_at="2026-07-11T13:05:00+08:00",
        )
        first = receive_draft_package(package, self.root / "inbox")
        second = receive_draft_package(package, self.root / "inbox")
        accepted = accept_received_draft(Path(first["receipt_path"]), self.collector_vault)
        repeated = accept_received_draft(Path(second["receipt_path"]), self.collector_vault)

        self.assertEqual(first["receipt_path"], second["receipt_path"])
        self.assertEqual(accepted["created"], 1)
        self.assertEqual(repeated["unchanged"], 1)

    def test_received_drafts_can_be_restored_after_restart(self) -> None:
        receipt = self.build_and_receive("restore-after-restart")

        restored = list_received_drafts(self.root / "inbox")

        self.assertEqual(
            restored,
            {
                "receipts": [
                    {"receipt_path": str(receipt), "draft_count": 1},
                ]
            },
        )

    def test_accepted_receipt_is_not_restored_as_pending(self) -> None:
        receipt = self.build_and_receive("accepted-not-pending")
        self.assertEqual(len(list_received_drafts(self.root / "inbox")["receipts"]), 1)

        accept_received_draft(receipt, self.collector_vault)

        self.assertEqual(list_received_drafts(self.root / "inbox"), {"receipts": []})
        self.assertTrue((receipt / ".accepted").is_file())

    def test_secret_and_traversal_packages_are_rejected(self) -> None:
        self.write_draft("Cookie: sid=secret")
        with self.assertRaises(DraftPackageError):
            build_draft_package(
                self.reader_vault,
                ["10-编辑稿/稿件.md"],
                self.root / "secret.inno-drafts",
                exported_at="2026-07-11T13:05:00+08:00",
            )

        bad = self.root / "bad.inno-drafts"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("draft-manifest.json", json.dumps({}))
            archive.writestr("payload/../escape", "x")
        with self.assertRaises(DraftPackageError):
            receive_draft_package(bad, self.root / "inbox")

    def test_extra_frontmatter_field_is_rejected(self) -> None:
        text = self.draft.read_text(encoding="utf-8")
        self.draft.write_text(
            text.replace("---\n\n", 'unexpected: "value"\n---\n\n', 1),
            encoding="utf-8",
        )

        with self.assertRaises(DraftPackageError):
            build_draft_package(
                self.reader_vault,
                ["10-编辑稿/稿件.md"],
                self.root / "extra.inno-drafts",
                exported_at="2026-07-11T13:05:00+08:00",
            )

    def test_conflicting_attachment_rejects_before_writing_conflict_draft(self) -> None:
        first = self.build_and_receive("first")
        accept_received_draft(first, self.collector_vault)
        self.write_draft("冲突人工稿")
        attachment = self.reader_vault / "10-编辑稿/附件/draft-one/image.png"
        attachment.write_bytes(b"different")
        second = self.build_and_receive("second")
        before = {
            path.relative_to(self.collector_vault).as_posix(): path.read_bytes()
            for path in self.collector_vault.rglob("*")
            if path.is_file()
        }

        with self.assertRaises(DraftPackageError):
            accept_received_draft(second, self.collector_vault)

        after = {
            path.relative_to(self.collector_vault).as_posix(): path.read_bytes()
            for path in self.collector_vault.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
