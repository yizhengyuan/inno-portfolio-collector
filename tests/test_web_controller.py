from __future__ import annotations

import http.client
import tempfile
import unittest
from pathlib import Path

from inno_collector.web.controller import WebController
from inno_collector.web.server import LocalWebServer, WebResponse


class WebControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"

    def test_bootstrap_returns_only_safe_read_only_state(self) -> None:
        controller = WebController(
            self.vault,
            authenticated=lambda: True,
            recent_job=lambda: {
                "id": "job-1",
                "status": "partial",
                "summary": "saved under /Users/private token=secret",
                "runtime_dir": "/Users/private/runtime",
                "auth-key": "secret",
            },
        )

        status, payload = controller("GET", "/api/bootstrap", None)

        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], "0.1.0")
        self.assertIs(payload["authenticated"], True)
        self.assertEqual(payload["capabilities"], ["read_library"])
        self.assertEqual(payload["recent_job"]["id"], "job-1")
        serialized = repr(payload)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("runtime_dir", serialized)
        self.assertNotIn("auth-key", serialized)

    def test_missing_vault_returns_empty_library(self) -> None:
        controller = WebController(self.vault)

        status, payload = controller("GET", "/api/library/summary", None)

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "exists": False,
                "healthy": True,
                "article_count": 0,
                "project_count": 0,
                "failed_projects": 0,
                "issue_count": 0,
            },
        )

    def test_existing_vault_summary_reuses_linter_without_exposing_details(self) -> None:
        self.vault.mkdir()
        calls: list[Path] = []

        def linter(path: Path) -> dict[str, object]:
            calls.append(path)
            return {
                "article_count": 225,
                "project_count": 10,
                "failed_projects": 8,
                "errors": ["/Users/private/bad.md token=secret"],
            }

        controller = WebController(self.vault, linter=linter)

        status, payload = controller("GET", "/api/library/summary", None)

        self.assertEqual(status, 200)
        self.assertEqual(calls, [self.vault])
        self.assertEqual(payload["article_count"], 225)
        self.assertEqual(payload["project_count"], 10)
        self.assertEqual(payload["failed_projects"], 8)
        self.assertEqual(payload["issue_count"], 1)
        self.assertIs(payload["healthy"], False)
        self.assertNotIn("errors", payload)

    def test_static_assets_are_fixed_and_unknown_paths_are_rejected(self) -> None:
        controller = WebController(self.vault)

        for path, content_type in (
            ("/", "text/html; charset=utf-8"),
            ("/assets/app.css", "text/css; charset=utf-8"),
            ("/assets/app.js", "text/javascript; charset=utf-8"),
        ):
            with self.subTest(path=path):
                response = controller("GET", path, None)
                self.assertIsInstance(response, WebResponse)
                self.assertEqual(response.content_type, content_type)

        for path in ("/assets/../controller.py", "/assets/unknown.svg", "/favicon.ico"):
            with self.subTest(path=path):
                status, payload = controller("GET", path, None)
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"]["code"], "not_found")

    def test_symlinked_asset_is_rejected(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        outside = self.root / "outside.css"
        outside.write_text("secret", encoding="utf-8")
        (assets / "app.css").symlink_to(outside)
        controller = WebController(self.vault, assets_root=assets)

        status, payload = controller("GET", "/assets/app.css", None)

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_server_serves_packaged_shell_and_injects_session_token(self) -> None:
        server = LocalWebServer(application=WebController(self.vault))
        server.start_background()
        self.addCleanup(server.stop)
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)

        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read()
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertIn(server.session_token.encode("ascii"), body)
        self.assertNotIn(b"__INNO_SESSION_TOKEN__", body)
        self.assertIn("英诺资讯采集".encode(), body)


if __name__ == "__main__":
    unittest.main()
