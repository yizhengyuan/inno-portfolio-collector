from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inno_collector.web.controller import WebController
from inno_collector.web.requests import UploadedFile
from inno_collector.web.uploads import DraftUploadError


class FakeDraftManager:
    def preview(self, filename: str, path: Path) -> dict[str, object]:
        if filename != "friend.inno-drafts":
            raise DraftUploadError(400, "invalid_draft_upload", "请选择稿件包。")
        self.preview_bytes = path.read_bytes()
        return {
            "receipt_id": "r" * 43,
            "draft_count": 1,
            "existing": False,
            "drafts": [{"title": "朋友稿件", "draft_version": 1}],
        }

    def accept(self, receipt_id: str, vault: Path, *, confirm: bool):
        if confirm is not True:
            raise DraftUploadError(409, "confirmation_required", "需要明确确认。")
        if receipt_id != "r" * 43:
            raise DraftUploadError(410, "preview_unavailable", "预览已失效。")
        return {
            "receipt_id": receipt_id,
            "accepted": True,
            "created": 1,
            "unchanged": 0,
            "conflicts": 0,
            "draft_count": 1,
        }


class WebDraftControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.manager = FakeDraftManager()
        self.controller = WebController(
            self.vault,
            draft_upload_manager=self.manager,
        )

    def test_preview_uses_staged_file_and_returns_opaque_receipt(self) -> None:
        path = self.root / "upload.tmp"
        path.write_bytes(b"draft-package")
        upload = UploadedFile(
            filename="friend.inno-drafts",
            content_type="application/octet-stream",
            path=path,
            size=path.stat().st_size,
        )

        status, payload = self.controller("POST", "/api/drafts/preview", upload)

        self.assertEqual(status, 200)
        self.assertEqual(payload["receipt_id"], "r" * 43)
        self.assertEqual(self.manager.preview_bytes, b"draft-package")
        self.assertNotIn(str(self.root), repr(payload))

    def test_accept_requires_explicit_confirmation_and_current_receipt(self) -> None:
        receipt_id = "r" * 43
        status, payload = self.controller(
            "POST", f"/api/drafts/{receipt_id}/accept", {"confirm": False}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "confirmation_required")

        status, accepted = self.controller(
            "POST", f"/api/drafts/{receipt_id}/accept", {"confirm": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(accepted["created"], 1)
        self.assertNotIn(str(self.root), repr(accepted))

        status, payload = self.controller(
            "POST", f"/api/drafts/{'x' * 43}/accept", {"confirm": True}
        )
        self.assertEqual(status, 410)
        self.assertEqual(payload["error"]["code"], "preview_unavailable")

    def test_frontend_requires_preview_and_explicit_confirmation(self) -> None:
        root = Path(__file__).parents[1] / "src/inno_collector/web/assets"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "app.js").read_text(encoding="utf-8")

        self.assertIn('accept=".inno-drafts"', html)
        self.assertIn('id="draft-confirm"', html)
        self.assertIn("/api/drafts/preview", javascript)
        self.assertIn("/api/drafts/${receiptId}/accept", javascript)


if __name__ == "__main__":
    unittest.main()
