from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inno_collector.exporter import ExporterCommandError
from inno_collector.web.controller import WebController
from inno_collector.web.server import WebResponse


LOGIN_ID = "b" * 32


class FakeLoginRuntime:
    def __init__(self) -> None:
        self.status = "waiting_for_scan"
        self.completed = False

    def auth_check(self) -> dict:
        return {"ok": True, "status": "valid"}

    def start_login(self, base_url: str) -> dict:
        self.base_url = base_url
        return {
            "login_id": LOGIN_ID,
            "expires_at": "2026-07-12T16:00:00+08:00",
            "qrcode_content_type": "image/png",
        }

    def read_qrcode(self, login_id: str) -> tuple[bytes, str]:
        if login_id != LOGIN_ID:
            raise ExporterCommandError("login session is unavailable")
        return b"png-data", "image/png"

    def login_status(self, login_id: str) -> dict:
        return {
            "login_id": login_id,
            "status": self.status,
            "status_code": 0,
            "acct_size": None,
            "message": "",
            "ready_to_complete": self.status == "confirmed",
        }

    def complete_login(self, login_id: str, profile: str = "") -> dict:
        self.completed = True
        return {
            "profile_id": 1,
            "nickname": "英诺",
            "auth-key": "must-not-leak",
        }


class WebLoginFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = FakeLoginRuntime()
        self.controller = WebController(
            Path(self.temp.name) / "vault",
            moore_runtime=self.runtime,
        )

    def test_start_qrcode_status_and_complete_stay_in_one_controller(self) -> None:
        status, started = self.controller("POST", "/api/login/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(started["login_id"], LOGIN_ID)
        self.assertNotIn("base_url", started)

        qrcode = self.controller("GET", f"/api/login/{LOGIN_ID}/qrcode", None)
        self.assertIsInstance(qrcode, WebResponse)
        self.assertEqual(qrcode.content_type, "image/png")
        self.assertEqual(qrcode.body, b"png-data")

        expected_messages = {
            "waiting_for_scan": "请使用微信扫描二维码。",
            "scanned_waiting_confirm": "已扫码，请在微信中确认登录。",
            "confirmed": "微信已确认，正在完成本机登录。",
            "expired": "二维码已过期，请重新生成。",
            "account_not_bound_email": "该微信账号未绑定可用的公众号后台账号。",
            "cancelled": "登录已取消，请重新开始。",
        }
        for runtime_status, message in expected_messages.items():
            with self.subTest(runtime_status=runtime_status):
                self.runtime.status = runtime_status
                response_status, payload = self.controller(
                    "GET", f"/api/login/{LOGIN_ID}/status", None
                )
                self.assertEqual(response_status, 200)
                self.assertEqual(payload["message_zh"], message)

        response_status, completed = self.controller(
            "POST", f"/api/login/{LOGIN_ID}/complete", {}
        )
        self.assertEqual(response_status, 200)
        self.assertTrue(self.runtime.completed)
        self.assertNotIn("auth-key", completed)
        self.assertNotIn("must-not-leak", repr(completed))

    def test_unknown_login_and_runtime_failure_use_safe_chinese_errors(self) -> None:
        status, payload = self.controller(
            "GET", f"/api/login/{'c' * 32}/qrcode", None
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "login_unavailable")
        self.assertIn("登录", payload["error"]["message"])

        self.runtime.start_login = lambda base_url: (_ for _ in ()).throw(  # type: ignore[method-assign]
            ExporterCommandError("token=secret /Users/private/runtime")
        )
        status, payload = self.controller("POST", "/api/login/start", {})
        self.assertEqual(status, 503)
        self.assertNotIn("secret", repr(payload))
        self.assertNotIn("/Users/", repr(payload))

    def test_frontend_polls_no_faster_than_two_seconds_and_auto_completes(self) -> None:
        javascript = (
            Path(__file__).parents[1]
            / "src/inno_collector/web/assets/app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/login/start", javascript)
        self.assertIn("/complete", javascript)
        self.assertIn("setTimeout", javascript)
        self.assertIn("2000", javascript)
        self.assertNotIn("auth-key", javascript.casefold())


if __name__ == "__main__":
    unittest.main()
