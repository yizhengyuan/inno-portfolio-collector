from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO

from .security import (
    LOOPBACK_HOST,
    MAX_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    SESSION_HEADER,
    SecurityError,
    security_headers,
    validate_bind_host,
    validate_host_header,
    validate_write_headers,
)
from .responses import WebResponse


Application = Callable[[str, str, object], tuple[int, object] | WebResponse]
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _not_found(_method: str, _path: str, _payload: object) -> tuple[int, object]:
    return 404, _error_payload("not_found", "Not found.")


def _error_payload(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class _BoundHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], owner: LocalWebServer) -> None:
        self.owner = owner
        super().__init__(address, _RequestHandler)


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        return "InnoCollector"

    @property
    def local_server(self) -> LocalWebServer:
        return self.server.owner  # type: ignore[attr-defined, no-any-return]

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _send_headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in security_headers().items():
            self.send_header(name, value)
        self.end_headers()

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        if len(body) > MAX_RESPONSE_BYTES:
            self._send_json(
                500,
                _error_payload("response_too_large", "Response exceeded the safe limit."),
            )
            return
        self._send_headers(status, content_type, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            body = json.dumps(
                _error_payload("internal_error", "The local application could not respond."),
                separators=(",", ":"),
            ).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            status = 500
            body = json.dumps(
                _error_payload("response_too_large", "Response exceeded the safe limit."),
                separators=(",", ":"),
            ).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_security_error(self, error: SecurityError) -> None:
        self._send_json(error.status, _error_payload(error.code, error.message))

    def _validate_host(self) -> bool:
        try:
            validate_host_header(self.headers.get("Host", ""), self.local_server.port)
        except SecurityError as error:
            self.close_connection = True
            self._send_security_error(error)
            return False
        return True

    def _read_json_body(self) -> object:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if transfer_encoding:
            raise SecurityError(400, "invalid_body", "Chunked requests are not accepted.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise SecurityError(411, "length_required", "Content-Length is required.")
        try:
            length = int(raw_length, 10)
        except ValueError:
            raise SecurityError(400, "invalid_body", "Content-Length is invalid.") from None
        if length < 0:
            raise SecurityError(400, "invalid_body", "Content-Length is invalid.")
        if length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise SecurityError(
                413,
                "request_too_large",
                "Request exceeded the safe limit.",
            )
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise SecurityError(400, "invalid_body", "Request body was incomplete.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SecurityError(400, "invalid_json", "Request JSON is invalid.") from None

    def _dispatch(self) -> None:
        if not self._validate_host():
            return
        method = self.command
        payload: object = None
        if method in _WRITE_METHODS:
            try:
                validate_write_headers(
                    content_type=self.headers.get("Content-Type", ""),
                    origin=self.headers.get("Origin", ""),
                    token=self.headers.get(SESSION_HEADER, ""),
                    expected_origin=self.local_server.origin,
                    expected_token=self.local_server.session_token,
                )
                payload = self._read_json_body()
            except SecurityError as error:
                self.close_connection = True
                self._send_security_error(error)
                return

        if method in {"GET", "HEAD"} and self.path == "/health":
            self._send_json(200, {"ok": True, "status": "ready"})
            return

        try:
            if method in _WRITE_METHODS:
                with self.local_server.write_lock:
                    result = self.local_server.application(
                        method, self.path, payload
                    )
            else:
                result = self.local_server.application(
                    method, self.path, payload
                )
            if isinstance(result, WebResponse):
                body = result.body
                if result.inject_session_token:
                    marker = b"__INNO_SESSION_TOKEN__"
                    if marker not in body:
                        raise ValueError("missing session token marker")
                    body = body.replace(
                        marker,
                        self.local_server.session_token.encode("ascii"),
                        1,
                    )
                self._send_bytes(result.status, result.content_type, body)
                return
            status, response = result
            if method in {"GET", "HEAD"} and self.path == "/" and status == 404:
                token = self.local_server.session_token
                body = (
                    "<!doctype html><html><head><meta charset=\"utf-8\">"
                    f"<meta name=\"inno-session-token\" content=\"{token}\">"
                    "<title>Inno Collector</title></head><body></body></html>"
                ).encode("utf-8")
                self._send_bytes(200, "text/html; charset=utf-8", body)
                return
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("invalid application status")
            self._send_json(status, response)
        except Exception:
            self._send_json(
                500,
                _error_payload(
                    "internal_error",
                    "The local application could not respond.",
                ),
            )

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()


class LocalWebServer:
    def __init__(
        self,
        *,
        host: str = LOOPBACK_HOST,
        port: int = 0,
        application: Application | None = None,
    ) -> None:
        self.host = validate_bind_host(host)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.application = application or _not_found
        self.session_token = secrets.token_urlsafe(32)
        self.write_lock = threading.Lock()
        self._httpd = _BoundHTTPServer((self.host, port), self)
        self.port = int(self._httpd.server_address[1])
        self.origin = f"http://{self.host}:{self.port}"
        self._thread: threading.Thread | None = None
        self._serving = threading.Event()
        self._stopped = False

    @property
    def ready_payload(self) -> dict[str, int | str]:
        return {
            "protocol": 1,
            "host": self.host,
            "port": self.port,
            "pid": os.getpid(),
        }

    def write_ready(self, stream: IO[str] = sys.stdout) -> None:
        stream.write(
            json.dumps(
                self.ready_payload,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        stream.flush()

    def serve_forever(self, ready_stream: IO[str] | None = sys.stdout) -> None:
        if ready_stream is not None:
            self.write_ready(ready_stream)
        self._serving.set()
        try:
            self._httpd.serve_forever(poll_interval=0.1)
        finally:
            self._serving.clear()

    def start_background(self) -> None:
        if self._stopped:
            raise RuntimeError("server has already stopped")
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"ready_stream": None},
            name=f"inno-web-{self.port}",
            daemon=True,
        )
        self._thread.start()
        if not self._serving.wait(timeout=2):
            raise RuntimeError("server failed to start")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._serving.is_set():
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Inno Collector Web server")
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    support_root = Path(
        os.environ.get(
            "INNO_COLLECTOR_SUPPORT_ROOT",
            "~/Library/Application Support/com.inno.news.collector",
        )
    ).expanduser()
    vault = support_root / "Runtime" / "vault" / "英诺被投项目资讯库"
    from .controller import WebController
    from .moore_runtime import MooreRuntime

    server = LocalWebServer(
        host=arguments.host,
        port=arguments.port,
        application=WebController(
            vault,
            moore_runtime=MooreRuntime(support_root / "ExporterRuntime"),
            runtime_dir=support_root / "Runtime",
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
