from __future__ import annotations

import unittest

from inno_collector.web.security import (
    MAX_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    SecurityError,
    security_headers,
    validate_bind_host,
    validate_host_header,
    validate_write_headers,
)


class WebSecurityTests(unittest.TestCase):
    def test_only_ipv4_loopback_can_be_bound(self) -> None:
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        for host in ("localhost", "0.0.0.0", "::1", "192.168.1.5", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_bind_host(host)

    def test_host_header_must_match_dynamic_loopback_origin(self) -> None:
        validate_host_header("127.0.0.1:54321", 54321)
        for host in ("localhost:54321", "127.0.0.1:1", "evil.test", ""):
            with self.subTest(host=host), self.assertRaises(SecurityError) as raised:
                validate_host_header(host, 54321)
            self.assertEqual(raised.exception.status, 421)

    def test_write_headers_require_json_current_origin_and_token(self) -> None:
        validate_write_headers(
            content_type="application/json; charset=utf-8",
            origin="http://127.0.0.1:54321",
            token="current-token",
            expected_origin="http://127.0.0.1:54321",
            expected_token="current-token",
        )
        cases = (
            ({"content_type": "text/plain"}, 415),
            ({"origin": "https://evil.test"}, 403),
            ({"origin": ""}, 403),
            ({"token": "wrong"}, 403),
            ({"token": ""}, 403),
        )
        defaults = {
            "content_type": "application/json",
            "origin": "http://127.0.0.1:54321",
            "token": "current-token",
            "expected_origin": "http://127.0.0.1:54321",
            "expected_token": "current-token",
        }
        for changes, status in cases:
            with self.subTest(changes=changes), self.assertRaises(SecurityError) as raised:
                validate_write_headers(**{**defaults, **changes})
            self.assertEqual(raised.exception.status, status)

    def test_default_headers_are_locked_down_without_cors(self) -> None:
        headers = security_headers()

        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_body_and_response_limits_are_finite(self) -> None:
        self.assertGreater(MAX_REQUEST_BODY_BYTES, 0)
        self.assertLessEqual(MAX_REQUEST_BODY_BYTES, 4 << 20)
        self.assertGreater(MAX_RESPONSE_BYTES, 0)
        self.assertLessEqual(MAX_RESPONSE_BYTES, 8 << 20)


if __name__ == "__main__":
    unittest.main()
