from __future__ import annotations

import http.client
import io
import json
import os
import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from inno_collector.web.security import MAX_REQUEST_BODY_BYTES, MAX_RESPONSE_BYTES
from inno_collector.web.requests import UploadedFile
from inno_collector.web.responses import FileResponse
from inno_collector.web.server import (
    LocalWebServer,
    _argument_parser,
    _launcher_pid_from_environment,
    _process_is_alive,
    _ready_process_id,
)


class TestApplication:
    def __init__(self) -> None:
        self.active_writes = 0
        self.max_active_writes = 0
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.download_path = None
        self.download_completed: list[bool] = []
        self.download_complete_event = threading.Event()

    def record_download_completed(self, success: bool) -> None:
        self.download_completed.append(success)
        self.download_complete_event.set()

    def __call__(self, method: str, path: str, payload: object) -> tuple[int, object]:
        if path == "/api/echo" and method == "POST":
            return 200, {"ok": True, "payload": payload}
        if path == "/api/fail":
            raise RuntimeError("token=secret /Users/private/runtime.json")
        if path == "/api/large":
            return 200, {"data": "x" * MAX_RESPONSE_BYTES}
        if path == "/api/serialized" and method == "POST":
            with self.lock:
                self.active_writes += 1
                self.max_active_writes = max(
                    self.max_active_writes, self.active_writes
                )
            self.release.wait(timeout=1)
            with self.lock:
                self.active_writes -= 1
            return 200, {"ok": True}
        if path == "/api/drafts/preview" and method == "POST":
            if not isinstance(payload, UploadedFile):
                raise TypeError("expected upload")
            return 200, {
                "ok": True,
                "filename": payload.filename,
                "size": payload.size,
                "contents": payload.path.read_text(encoding="utf-8"),
            }
        if path == "/api/download" and method == "GET" and self.download_path:
            data = self.download_path.read_bytes()
            return FileResponse(
                path=self.download_path,
                filename="英诺更新.inno-update",
                content_type="application/octet-stream",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                on_complete=self.record_download_completed,
            )
        return 404, {"ok": False, "error": {"code": "not_found", "message": "Not found"}}


class LocalWebServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = TestApplication()
        self.server = LocalWebServer(application=self.application)
        self.server.start_background()
        self.addCleanup(self.server.stop)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        response_headers = {key: value for key, value in response.getheaders()}
        connection.close()
        return response.status, response_headers, data

    def write_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Origin": self.server.origin,
            "X-Inno-Session": self.server.session_token,
        }

    def test_ready_handshake_contains_no_token_or_path(self) -> None:
        stream = io.StringIO()

        self.server.write_ready(stream)

        raw = stream.getvalue()
        self.assertEqual(raw.count("\n"), 1)
        payload = json.loads(raw)
        self.assertEqual(
            payload,
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": self.server.port,
                "pid": os.getpid(),
            },
        )
        self.assertNotIn(self.server.session_token, raw)
        self.assertNotIn("/Users/", raw)

    def test_onefile_ready_identifies_the_launcher_owned_process(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(os.environ, {"_PYI_PARENT_PROCESS_LEVEL": "1"}),
            patch("inno_collector.web.server.os.getppid", return_value=4321),
        ):
            self.assertEqual(_ready_process_id(), 4321)

    def test_launcher_pid_environment_accepts_only_one_canonical_pid(self) -> None:
        self.assertEqual(
            _launcher_pid_from_environment({"INNO_COLLECTOR_LAUNCHER_PID": "4321"}),
            4321,
        )
        self.assertIsNone(_launcher_pid_from_environment({}))

        for value in (
            "",
            "0",
            "1",
            "01",
            "+4321",
            " 4321",
            "4321 ",
            "-1",
            "2147483648",
            "not-a-pid",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _launcher_pid_from_environment(
                        {"INNO_COLLECTOR_LAUNCHER_PID": value}
                    )

    def test_process_liveness_probe_never_sends_a_terminating_signal(self) -> None:
        with patch("inno_collector.web.server.os.kill") as kill:
            self.assertTrue(_process_is_alive(4321))

        kill.assert_called_once_with(4321, 0)

    def test_command_line_accepts_only_explicit_local_runtime_inputs(self) -> None:
        arguments = _argument_parser().parse_args(
            [
                "--support-root",
                "/tmp/inno-support",
                "--projects",
                "/tmp/InnoCollector.app/Contents/Resources/config/projects.json",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ]
        )

        self.assertEqual(arguments.support_root, Path("/tmp/inno-support"))
        self.assertEqual(
            arguments.projects,
            Path("/tmp/InnoCollector.app/Contents/Resources/config/projects.json"),
        )
        self.assertEqual(arguments.host, "127.0.0.1")
        self.assertEqual(arguments.port, 0)

    def test_root_injects_token_only_into_same_origin_html(self) -> None:
        status, headers, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(self.server.session_token.encode(), body)
        health_status, _, health = self.request("GET", "/health")
        self.assertEqual(health_status, 200)
        self.assertNotIn(self.server.session_token.encode(), health)

    def test_nonlocal_host_is_misdirected(self) -> None:
        status, _, body = self.request("GET", "/health", headers={"Host": "evil.test"})

        self.assertEqual(status, 421)
        self.assertEqual(json.loads(body)["error"]["code"], "misdirected_request")

    def test_writes_require_json_origin_and_session_token(self) -> None:
        body = b'{"value": 7}'
        status, _, response = self.request(
            "POST", "/api/echo", body=body, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["payload"], {"value": 7})

        for removed, expected in (
            ("Content-Type", 415),
            ("Origin", 403),
            ("X-Inno-Session", 403),
        ):
            headers = self.write_headers()
            headers.pop(removed)
            with self.subTest(removed=removed):
                status, _, _ = self.request("POST", "/api/echo", body=body, headers=headers)
                self.assertEqual(status, expected)

    def test_malformed_json_and_large_body_are_stable_errors(self) -> None:
        status, _, body = self.request(
            "POST", "/api/echo", body=b"{", headers=self.write_headers()
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_json")

        headers = {**self.write_headers(), "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}
        status, _, body = self.request("POST", "/api/echo", body=b"", headers=headers)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "request_too_large")

    def test_draft_preview_accepts_one_bounded_multipart_file(self) -> None:
        boundary = "inno-boundary"
        upload = b"draft-package"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="friend.inno-drafts"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + upload + f"\r\n--{boundary}--\r\n".encode()
        headers = {
            **self.write_headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }

        status, _, response = self.request(
            "POST", "/api/drafts/preview", body=body, headers=headers
        )

        self.assertEqual(status, 200)
        payload = json.loads(response)
        self.assertEqual(payload["filename"], "friend.inno-drafts")
        self.assertEqual(payload["size"], len(upload))
        self.assertEqual(payload["contents"], upload.decode())
        self.assertEqual(list(self.server.upload_root.iterdir()), [])

    def test_draft_preview_rejects_multiple_files_and_path_filenames(self) -> None:
        boundary = "inno-boundary"

        def part(filename: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
                "payload\r\n"
            ).encode()

        headers = {
            **self.write_headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        for body in (
            part("one.inno-drafts") + part("two.inno-drafts") + f"--{boundary}--\r\n".encode(),
            part("../secret.inno-drafts") + f"--{boundary}--\r\n".encode(),
        ):
            with self.subTest(size=len(body)):
                status, _, response = self.request(
                    "POST", "/api/drafts/preview", body=body, headers=headers
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(response)["error"]["code"], "invalid_multipart")

    def test_unknown_route_internal_error_and_large_response_are_sanitized(self) -> None:
        status, _, body = self.request("GET", "/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

        status, _, body = self.request("GET", "/api/fail")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "internal_error")
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"/Users/", body)

        status, _, body = self.request("GET", "/api/large")
        self.assertEqual(status, 500)
        self.assertLess(len(body), 1024)

    def test_registered_file_response_has_verified_download_headers(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "update.inno-update"
        path.write_bytes(b"verified-package")
        self.application.download_path = path

        status, headers, body = self.request("GET", "/api/download")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"verified-package")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("filename*=UTF-8", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-SHA256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(self.application.download_complete_event.wait(timeout=1))
        self.assertEqual(self.application.download_completed, [True])

    def test_all_responses_have_security_headers_and_no_cors(self) -> None:
        _, headers, _ = self.request("GET", "/health")

        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_writes_are_serialized(self) -> None:
        results: list[int] = []

        def perform() -> None:
            status, _, _ = self.request(
                "POST", "/api/serialized", body=b"{}", headers=self.write_headers()
            )
            results.append(status)

        first = threading.Thread(target=perform)
        second = threading.Thread(target=perform)
        first.start()
        second.start()
        threading.Event().wait(0.05)
        self.application.release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(sorted(results), [200, 200])
        self.assertEqual(self.application.max_active_writes, 1)

    def test_stopping_one_server_does_not_stop_another(self) -> None:
        other = LocalWebServer(application=self.application)
        other.start_background()
        self.addCleanup(other.stop)

        self.server.stop()

        connection = http.client.HTTPConnection("127.0.0.1", other.port, timeout=2)
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 200)

    def test_launcher_watchdog_stops_only_its_server_and_closes_the_port(self) -> None:
        launcher_pid = os.getpid() + 1
        launcher_alive = threading.Event()
        launcher_alive.set()
        launcher_checked = threading.Event()

        def process_is_alive(pid: int) -> bool:
            self.assertEqual(pid, launcher_pid)
            alive = launcher_alive.is_set()
            if not alive:
                launcher_checked.set()
            return alive

        watched = LocalWebServer(
            application=self.application,
            launcher_pid=launcher_pid,
            process_is_alive=process_is_alive,
            launcher_watchdog_interval=0.01,
        )
        watched.start_background()
        other = LocalWebServer(application=self.application)
        other.start_background()
        self.addCleanup(watched.stop)
        self.addCleanup(other.stop)

        watched_port = watched.port
        launcher_alive.clear()

        self.assertTrue(launcher_checked.wait(timeout=1))
        self.assertTrue(watched.wait_stopped(timeout=1))
        with self.assertRaises(OSError):
            connection = http.client.HTTPConnection(
                "127.0.0.1", watched_port, timeout=0.2
            )
            try:
                connection.request("GET", "/health")
                connection.getresponse()
            finally:
                connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", other.port, timeout=2)
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 200)

    def test_launcher_watchdog_rejects_invalid_or_dead_pid_before_binding(self) -> None:
        for pid in (1, os.getpid(), 2_147_483_648):
            with self.subTest(pid=pid):
                with self.assertRaises(ValueError):
                    LocalWebServer(
                        application=self.application,
                        launcher_pid=pid,
                        process_is_alive=lambda _pid: True,
                    )

        with self.assertRaises(ValueError):
            LocalWebServer(
                application=self.application,
                launcher_pid=os.getpid() + 1,
                process_is_alive=lambda _pid: False,
            )

    def test_stop_is_safe_before_server_starts(self) -> None:
        dormant = LocalWebServer(application=self.application)

        dormant.stop()
        dormant.stop()


if __name__ == "__main__":
    unittest.main()
