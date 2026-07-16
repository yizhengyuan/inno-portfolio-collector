from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from inno_collector.draft_package import (
    DraftPackageError,
    build_draft_package,
    receive_draft_package,
)
from inno_collector.vault import VaultWriter
from inno_collector.web.uploads import DraftUploadError, DraftUploadManager


SOURCE_ID = "sha256:" + "1" * 64


class DraftUploadManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.reader_vault = self.root / "reader"
        self.collector_vault = self.root / "collector"
        VaultWriter(self.reader_vault).apply([], [])
        VaultWriter(self.collector_vault).apply([], [])
        self.draft = self.reader_vault / "10-编辑稿/稿件.md"
        self.write_draft("首版人工稿")

    def write_draft(self, body: str) -> None:
        self.draft.write_text(
            "---\n"
            'draft_id: "draft-one"\n'
            "draft_version: 1\n"
            'author: "朋友甲"\n'
            'title: "项目选题稿"\n'
            'updated_at: "2026-07-12T09:00:00+08:00"\n'
            f'source_ids: ["{SOURCE_ID}"]\n'
            "---\n\n"
            f"{body}\n",
            encoding="utf-8",
        )

    def package_bytes(self, name: str) -> bytes:
        package = self.root / f"{name}.inno-drafts"
        build_draft_package(
            self.reader_vault,
            ["10-编辑稿/稿件.md"],
            package,
            exported_at="2026-07-12T09:05:00+08:00",
        )
        return package.read_bytes()

    def test_preview_stages_under_inbox_returns_only_opaque_safe_summary(self) -> None:
        payload = self.package_bytes("preview")
        calls: list[tuple[Path, Path]] = []

        def receiver(package: Path, inbox: Path) -> dict[str, object]:
            self.assertTrue(package.is_file())
            calls.append((package, inbox))
            return receive_draft_package(package, inbox)

        manager = DraftUploadManager(
            self.root / "inbox",
            receiver=receiver,
        )
        before = {
            path.relative_to(self.collector_vault).as_posix(): path.read_bytes()
            for path in self.collector_vault.rglob("*")
            if path.is_file()
        }

        result = manager.preview("friend.inno-drafts", payload)

        after = {
            path.relative_to(self.collector_vault).as_posix(): path.read_bytes()
            for path in self.collector_vault.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertRegex(result["receipt_id"], r"^[A-Za-z0-9_-]{32,64}$")
        self.assertEqual(result["draft_count"], 1)
        self.assertIs(result["existing"], False)
        self.assertEqual(
            result["drafts"],
            [
                {
                    "draft_id": "draft-one",
                    "draft_version": 1,
                    "author": "朋友甲",
                    "title": "项目选题稿",
                    "updated_at": "2026-07-12T09:00:00+08:00",
                    "source_count": 1,
                    "attachment_count": 0,
                }
            ],
        )
        serialized = repr(result)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("receipt_path", serialized)
        self.assertEqual(len(calls), 1)
        staged, receipt_root = calls[0]
        self.assertEqual(staged.parents[1], self.root / "inbox" / ".uploads")
        self.assertEqual(receipt_root, self.root / "inbox" / "receipts")
        self.assertFalse(staged.exists())

    def test_only_one_inno_drafts_file_is_accepted(self) -> None:
        manager = DraftUploadManager(self.root / "inbox")
        payload = self.package_bytes("only-one")

        for filename in ("draft.zip", "draft.inno-drafts.exe", "../draft.inno-drafts"):
            with self.subTest(filename=filename), self.assertRaises(DraftUploadError) as caught:
                manager.preview(filename, payload)
            self.assertEqual(caught.exception.code, "invalid_draft_upload")

        for uploads in ([], [("a.inno-drafts", payload), ("b.inno-drafts", payload)]):
            with self.subTest(count=len(uploads)), self.assertRaises(DraftUploadError) as caught:
                manager.preview_uploads(uploads)
            self.assertEqual(caught.exception.code, "invalid_upload_count")

    def test_total_and_single_file_limits_are_enforced_while_streaming(self) -> None:
        single = DraftUploadManager(
            self.root / "single-inbox",
            max_file_bytes=5,
            max_total_bytes=20,
        )
        with self.assertRaises(DraftUploadError) as caught:
            single.preview("draft.inno-drafts", [b"123", b"456"])
        self.assertEqual(caught.exception.code, "upload_too_large")

        total = DraftUploadManager(
            self.root / "total-inbox",
            max_file_bytes=20,
            max_total_bytes=5,
        )
        with self.assertRaises(DraftUploadError) as caught:
            total.preview("draft.inno-drafts", b"123456")
        self.assertEqual(caught.exception.code, "upload_too_large")
        self.assertEqual(list((self.root / "single-inbox" / ".uploads").iterdir()), [])
        self.assertEqual(list((self.root / "total-inbox" / ".uploads").iterdir()), [])

    def test_path_payload_is_copied_without_following_symlinks(self) -> None:
        source = self.root / "request-upload.tmp"
        source.write_bytes(self.package_bytes("path-source"))
        manager = DraftUploadManager(self.root / "inbox")

        preview = manager.preview("friend.inno-drafts", source)

        self.assertEqual(preview["draft_count"], 1)
        linked = self.root / "linked-upload.tmp"
        linked.symlink_to(source)
        with self.assertRaises(DraftUploadError) as caught:
            manager.preview("linked.inno-drafts", linked)
        self.assertEqual(caught.exception.code, "invalid_draft_upload")

    def test_zip_member_and_uncompressed_budgets_stop_bombs_before_receive(self) -> None:
        too_many = self.root / "too-many.zip"
        with zipfile.ZipFile(too_many, "w") as archive:
            archive.writestr("one", b"1")
            archive.writestr("two", b"2")
        manager = DraftUploadManager(
            self.root / "member-inbox",
            max_archive_members=1,
        )
        with self.assertRaises(DraftUploadError) as caught:
            manager.preview("many.inno-drafts", too_many)
        self.assertEqual(caught.exception.code, "invalid_draft_package")

        expanded = self.root / "expanded.zip"
        with zipfile.ZipFile(expanded, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large", b"x" * 64)
        manager = DraftUploadManager(
            self.root / "expanded-inbox",
            max_uncompressed_bytes=32,
        )
        with self.assertRaises(DraftUploadError) as caught:
            manager.preview("expanded.inno-drafts", expanded)
        self.assertEqual(caught.exception.code, "invalid_draft_package")

    def test_symlinked_inbox_and_receipt_escape_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked-inbox"
        linked.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(DraftUploadError) as caught:
            DraftUploadManager(linked)
        self.assertEqual(caught.exception.code, "unsafe_draft_inbox")

        def escaping_receiver(_package: Path, inbox: Path) -> dict[str, object]:
            escaped = inbox / ("a" * 64)
            escaped.symlink_to(outside, target_is_directory=True)
            return {"receipt_path": str(escaped), "draft_count": 1, "existing": False}

        manager = DraftUploadManager(
            self.root / "safe-inbox",
            receiver=escaping_receiver,
        )
        with self.assertRaises(DraftUploadError) as caught:
            manager.preview("draft.inno-drafts", b"payload")
        self.assertEqual(caught.exception.code, "invalid_draft_package")

    def test_preview_and_accept_errors_are_stable_and_do_not_leak_paths(self) -> None:
        def failing_receiver(_package: Path, _inbox: Path) -> dict[str, object]:
            raise DraftPackageError(
                f"bad package at {self.root}/secret token=do-not-show"
            )

        manager = DraftUploadManager(self.root / "inbox", receiver=failing_receiver)
        with self.assertRaises(DraftUploadError) as caught:
            manager.preview("draft.inno-drafts", b"not-a-package")

        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, "invalid_draft_package")
        self.assertNotIn(str(self.root), caught.exception.message)
        self.assertNotIn("do-not-show", caught.exception.message)

    def test_accept_requires_current_receipt_and_literal_confirmation(self) -> None:
        manager = DraftUploadManager(self.root / "inbox")
        preview = manager.preview("draft.inno-drafts", self.package_bytes("confirm"))

        for confirmation in (False, 1, "yes", None):
            with self.subTest(confirmation=confirmation), self.assertRaises(
                DraftUploadError
            ) as caught:
                manager.accept(
                    preview["receipt_id"],
                    self.collector_vault,
                    confirm=confirmation,
                )
            self.assertEqual(caught.exception.code, "confirmation_required")

        with self.assertRaises(DraftUploadError) as caught:
            manager.accept("not-current", self.collector_vault, confirm=True)
        self.assertEqual(caught.exception.code, "preview_unavailable")

        accepted = manager.accept(
            preview["receipt_id"],
            self.collector_vault,
            confirm=True,
        )
        self.assertEqual(
            accepted,
            {
                "receipt_id": preview["receipt_id"],
                "accepted": True,
                "created": 1,
                "unchanged": 0,
                "conflicts": 0,
                "draft_count": 1,
            },
        )

    def test_repeated_package_and_content_conflict_keep_domain_semantics(self) -> None:
        manager = DraftUploadManager(self.root / "inbox")
        first_payload = self.package_bytes("first")
        first = manager.preview("first.inno-drafts", first_payload)
        manager.accept(first["receipt_id"], self.collector_vault, confirm=True)

        repeated = manager.preview("again.inno-drafts", first_payload)
        self.assertEqual(repeated["receipt_id"], first["receipt_id"])
        self.assertIs(repeated["existing"], True)
        repeated_accept = manager.accept(
            repeated["receipt_id"], self.collector_vault, confirm=True
        )
        self.assertEqual(repeated_accept["unchanged"], 1)

        self.write_draft("冲突人工稿")
        conflict = manager.preview(
            "conflict.inno-drafts", self.package_bytes("conflict")
        )
        conflict_accept = manager.accept(
            conflict["receipt_id"], self.collector_vault, confirm=True
        )
        self.assertEqual(conflict_accept["conflicts"], 1)
        drafts = list((self.collector_vault / "10-编辑稿").glob("*.md"))
        self.assertEqual(len(drafts), 2)
        bodies = "\n".join(path.read_text(encoding="utf-8") for path in drafts)
        self.assertIn("首版人工稿", bodies)
        self.assertIn("冲突人工稿", bodies)

    def test_receipt_ttl_and_quantity_cleanup_make_old_ids_unavailable(self) -> None:
        now = [100.0]
        manager = DraftUploadManager(
            self.root / "inbox",
            max_receipts=1,
            receipt_ttl_seconds=10,
            clock=lambda: now[0],
        )
        first = manager.preview("first.inno-drafts", self.package_bytes("first"))
        self.write_draft("第二个包")
        second = manager.preview("second.inno-drafts", self.package_bytes("second"))

        with self.assertRaises(DraftUploadError) as caught:
            manager.accept(first["receipt_id"], self.collector_vault, confirm=True)
        self.assertEqual(caught.exception.code, "preview_unavailable")
        receipt_directories = [
            path for path in (self.root / "inbox" / "receipts").iterdir() if path.is_dir()
        ]
        self.assertEqual(len(receipt_directories), 1)

        now[0] += 11
        self.assertEqual(manager.cleanup(), 1)
        with self.assertRaises(DraftUploadError) as caught:
            manager.accept(second["receipt_id"], self.collector_vault, confirm=True)
        self.assertEqual(caught.exception.code, "preview_unavailable")
        self.assertEqual(list((self.root / "inbox" / "receipts").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
