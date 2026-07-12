from __future__ import annotations

import http.client
import io
import json
import os
import threading
import unittest

from inno_collector.web.security import MAX_REQUEST_BODY_BYTES, MAX_RESPONSE_BYTES
from inno_collector.web.server import LocalWebServer


class TestApplication:
    def __init__(self) -> None:
        self.active_writes = 0
        self.max_active_writes = 0
        self.lock = threading.Lock()
        self.release = threading.Event()

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

    def test_stop_is_safe_before_server_starts(self) -> None:
        dormant = LocalWebServer(application=self.application)

        dormant.stop()
        dormant.stop()


if __name__ == "__main__":
    unittest.main()
