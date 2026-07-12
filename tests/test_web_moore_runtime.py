from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inno_collector.exporter import ExporterCommandError
from inno_collector.web.moore_runtime import MooreRuntime


LOGIN_ID = "a" * 32


class FakeMooreFunctions:
    def __init__(self) -> None:
        self.qrcode_path = ""
        self.status_payload: dict = {
            "ok": True,
            "login_id": LOGIN_ID,
            "status": "confirmed",
            "status_code": 1,
            "acct_size": 3,
            "message": "ready",
            "ready_to_complete": True,
            "auth-key": "must-not-leak",
            "local_path": "/Users/private/login.json",
        }
        self.complete_payload: dict = {
            "profile_id": 8,
            "display_name": "collector",
            "expires_at": "2026-07-16T00:00:00+00:00",
            "nickname": "英诺",
            "avatar": "https://example.invalid/avatar.png",
            "auth-key": "must-not-leak",
        }
        self.auth_payload: dict = {
            "ok": True,
            "status": "valid",
            "code": 0,
            "profile": "collector",
            "expires_at": "2026-07-16T00:00:00+00:00",
            "token": "must-not-leak",
        }

    def start_qr_login(self, base: Path, base_url: str) -> dict:
        return {
            "ok": True,
            "login_id": LOGIN_ID,
            "qrcode_path": self.qrcode_path,
            "expires_at": "2026-07-12T12:00:00+00:00",
            "base_url": base_url,
            "next_step": "do not expose",
        }

    def qr_login_status(self, base: Path, login_id: str) -> dict:
        return self.status_payload

    def complete_qr_login(self, base: Path, login_id: str, profile: str) -> dict:
        return self.complete_payload

    def auth_check(self, base: Path, profile: str) -> dict:
        return self.auth_payload

    def list_accounts(self, base: Path) -> list[dict]:
        return [{"id": 11, "nickname": "Alpha"}]

    def sync_account_articles(
        self, base: Path, account_id: int, limit: int, keyword: str, profile: str
    ) -> dict:
        return {
            "ok": True,
            "account_id": account_id,
            "fetched_count": 2,
            "upserted_count": 2,
            "errors": [],
        }

    def list_articles(
        self,
        base: Path,
        account_id: int,
        limit: int,
        keyword: str,
        collection_id: int,
        downloaded: str,
    ) -> list[dict]:
        return [{"id": 21, "account_id": account_id}]

    def download_articles(
        self,
        base: Path,
        article_ids: list[int],
        output_dir: str,
        no_assets: bool,
        account_nickname: str,
    ) -> dict:
        return {"ok": True, "selected_count": len(article_ids)}


class MooreRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime_dir = Path(self.temp.name) / "runtime"
        self.runtime_dir.mkdir()
        self.functions = FakeMooreFunctions()
        self.qrcode = self.runtime_dir / "login" / f"{LOGIN_ID}.png"
        self.qrcode.parent.mkdir()
        self.qrcode.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.functions.qrcode_path = str(self.qrcode)
        self.runtime = MooreRuntime(self.runtime_dir, functions=self.functions)

    def test_start_returns_only_opaque_metadata_and_registered_qrcode(self) -> None:
        result = self.runtime.start_login("http://127.0.0.1:3000")

        self.assertEqual(
            result,
            {
                "login_id": LOGIN_ID,
                "expires_at": "2026-07-12T12:00:00+00:00",
                "qrcode_content_type": "image/png",
            },
        )
        self.assertEqual(
            self.runtime.read_qrcode(LOGIN_ID),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
        )

    def test_qrcode_requires_current_registered_session(self) -> None:
        with self.assertRaisesRegex(ExporterCommandError, "login session is unavailable"):
            self.runtime.read_qrcode(LOGIN_ID)

    def test_start_rejects_qrcode_outside_runtime(self) -> None:
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"png")
        self.functions.qrcode_path = str(outside)

        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_start_rejects_qrcode_symlink(self) -> None:
        target = self.runtime_dir / "real.png"
        target.write_bytes(b"png")
        symlink = self.runtime_dir / "linked.png"
        symlink.symlink_to(target)
        self.functions.qrcode_path = str(symlink)

        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_start_rejects_qrcode_below_symlinked_directory(self) -> None:
        real_dir = self.runtime_dir / "real-login"
        real_dir.mkdir()
        target = real_dir / "code.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        linked_dir = self.runtime_dir / "linked-login"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        self.functions.qrcode_path = str(linked_dir / "code.png")

        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_start_rejects_missing_and_oversized_qrcode(self) -> None:
        missing = self.runtime_dir / "missing.png"
        self.functions.qrcode_path = str(missing)
        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

        oversized = self.runtime_dir / "oversized.png"
        oversized.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (2 << 20))
        self.functions.qrcode_path = str(oversized)
        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_status_is_allowlisted_and_drops_secrets_and_paths(self) -> None:
        self.runtime.start_login("http://127.0.0.1:3000")

        result = self.runtime.login_status(LOGIN_ID)

        self.assertEqual(
            result,
            {
                "login_id": LOGIN_ID,
                "status": "confirmed",
                "status_code": 1,
                "acct_size": 3,
                "message": "ready",
                "ready_to_complete": True,
            },
        )
        self.assertNotIn("auth-key", result)
        self.assertNotIn("local_path", result)

    def test_complete_is_allowlisted_and_removes_qrcode(self) -> None:
        self.runtime.start_login("http://127.0.0.1:3000")

        result = self.runtime.complete_login(LOGIN_ID, "collector")

        self.assertEqual(
            result,
            {
                "profile_id": 8,
                "display_name": "collector",
                "expires_at": "2026-07-16T00:00:00+00:00",
                "nickname": "英诺",
                "avatar": "https://example.invalid/avatar.png",
            },
        )
        self.assertNotIn("auth-key", result)
        self.assertFalse(self.qrcode.exists())
        with self.assertRaisesRegex(ExporterCommandError, "login session is unavailable"):
            self.runtime.read_qrcode(LOGIN_ID)

    def test_direct_collection_calls_preserve_validation(self) -> None:
        self.assertEqual(self.runtime.auth_check()["status"], "valid")
        self.assertNotIn("token", self.runtime.auth_check())
        self.assertEqual(self.runtime.accounts()[0]["id"], 11)
        self.assertEqual(self.runtime.sync(11)["upserted_count"], 2)
        self.assertEqual(self.runtime.articles(11)[0]["id"], 21)
        self.assertEqual(
            self.runtime.download([21], self.runtime_dir / "output")["selected_count"],
            1,
        )

    def test_malformed_direct_results_are_rejected(self) -> None:
        self.functions.auth_payload = {"ok": "true", "status": "valid"}
        with self.assertRaisesRegex(ExporterCommandError, "exporter command failed"):
            self.runtime.auth_check()

        self.functions.list_accounts = lambda base: ["not-an-object"]  # type: ignore[method-assign]
        with self.assertRaisesRegex(ExporterCommandError, "invalid accounts"):
            self.runtime.accounts()

    def test_upstream_exception_uses_stable_error_without_secret_or_path(self) -> None:
        def fail(base: Path, profile: str) -> dict:
            raise RuntimeError(f"token=super-secret at {base / 'profile.json'}")

        self.functions.auth_check = fail  # type: ignore[method-assign]

        with self.assertRaises(ExporterCommandError) as raised:
            self.runtime.auth_check()

        self.assertEqual(str(raised.exception), "local exporter operation failed")


if __name__ == "__main__":
    unittest.main()
