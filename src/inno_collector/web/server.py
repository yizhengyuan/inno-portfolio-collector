from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO
from urllib.parse import quote

from .security import (
    LOOPBACK_HOST,
    MAX_DOWNLOAD_BYTES,
    MAX_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILE_BYTES,
    SESSION_HEADER,
    SecurityError,
    security_headers,
    validate_bind_host,
    validate_host_header,
    validate_write_headers,
    validate_write_identity,
)
from .requests import MultipartError, parse_single_file_multipart
from .responses import FileResponse, WebResponse


Application = Callable[[str, str, object], tuple[int, object] | WebResponse]
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _ready_process_id() -> int:
    """Return the process identifier owned by the native launcher.

    PyInstaller's POSIX one-file bootloader keeps a parent process alive while
    the extracted Python child serves requests.  The launcher owns and stops
    that parent, so the ready handshake identifies it.  Development and
    one-directory processes continue to identify themselves normally.
    """
    if (
        getattr(sys, "frozen", False)
        and os.environ.get("_PYI_PARENT_PROCESS_LEVEL") == "1"
    ):
        return os.getppid()
    return os.getpid()


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

    def _send_headers(
        self,
        status: int,
        content_type: str,
        length: int,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in security_headers().items():
            self.send_header(name, value)
        for name, value in (extra or {}).items():
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

    def _send_file(self, response: FileResponse) -> None:
        callback = response.on_complete or (lambda _success: None)
        def notify(success: bool) -> None:
            try:
                callback(success)
            except Exception:
                pass

        if (
            not isinstance(response.filename, str)
            or not response.filename
            or len(response.filename.encode("utf-8")) > 255
            or "/" in response.filename
            or "\\" in response.filename
            or "\r" in response.filename
            or "\n" in response.filename
            or type(response.size) is not int
            or not 0 <= response.size <= MAX_DOWNLOAD_BYTES
            or not isinstance(response.sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", response.sha256) is None
            or response.content_type not in {
                "application/octet-stream",
                "application/zip",
            }
        ):
            notify(False)
            raise ValueError("invalid download response")
        descriptor = -1
        sent = False
        try:
            descriptor = os.open(
                response.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size != response.size:
                raise OSError("download file changed")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while True:
                    chunk = stream.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != response.sha256:
                    raise OSError("download file changed")
                stream.seek(0)
                self._send_headers(
                    200,
                    response.content_type,
                    response.size,
                    {
                        "Content-Disposition": (
                            "attachment; filename*=UTF-8''" + quote(response.filename)
                        ),
                        "X-Content-SHA256": response.sha256,
                    },
                )
                if self.command != "HEAD":
                    while True:
                        chunk = stream.read(1 << 20)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                sent = True
        except (BrokenPipeError, ConnectionResetError):
            sent = False
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            notify(sent and self.command != "HEAD")

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

    def _content_length(self, max_bytes: int) -> int:
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
        if length > max_bytes:
            self.close_connection = True
            raise SecurityError(
                413,
                "request_too_large",
                "Request exceeded the safe limit.",
            )
        return length

    def _read_body(self, max_bytes: int) -> bytes:
        length = self._content_length(max_bytes)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise SecurityError(400, "invalid_body", "Request body was incomplete.")
        return raw

    def _read_json_body(self) -> object:
        raw = self._read_body(MAX_REQUEST_BODY_BYTES)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SecurityError(400, "invalid_json", "Request JSON is invalid.") from None

    def _dispatch(self) -> None:
        if not self._validate_host():
            return
        method = self.command
        payload: object = None
        uploaded_file = None
        upload_lock_acquired = False
        if method in _WRITE_METHODS:
            try:
                content_type = self.headers.get("Content-Type", "")
                identity = {
                    "origin": self.headers.get("Origin", ""),
                    "token": self.headers.get(SESSION_HEADER, ""),
                    "expected_origin": self.local_server.origin,
                    "expected_token": self.local_server.session_token,
                }
                is_multipart_delivery = (
                    method == "POST"
                    and self.path == "/api/delivery"
                    and content_type.split(";", 1)[0].strip().lower()
                    == "multipart/form-data"
                )
                if method == "POST" and (
                    self.path == "/api/drafts/preview" or is_multipart_delivery
                ):
                    validate_write_identity(**identity)
                    if not self.local_server.upload_lock.acquire(blocking=False):
                        raise SecurityError(409, "upload_busy", "Another upload is active.")
                    upload_lock_acquired = True
                    length = self._content_length(MAX_UPLOAD_BYTES)
                    uploaded_file = parse_single_file_multipart(
                        content_type,
                        self.rfile,
                        length,
                        self.local_server.upload_root,
                        max_file_bytes=MAX_UPLOAD_FILE_BYTES,
                    )
                    payload = uploaded_file
                else:
                    validate_write_headers(content_type=content_type, **identity)
                    payload = self._read_json_body()
            except MultipartError:
                if upload_lock_acquired:
                    self.local_server.upload_lock.release()
                self.close_connection = True
                self._send_security_error(
                    SecurityError(400, "invalid_multipart", "Uploaded form is invalid.")
                )
                return
            except SecurityError as error:
                if uploaded_file is not None:
                    uploaded_file.cleanup()
                if upload_lock_acquired:
                    self.local_server.upload_lock.release()
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
            if isinstance(result, FileResponse):
                self._send_file(result)
                return
            status, response = result
            if isinstance(response, FileResponse):
                if status != 200:
                    raise ValueError("invalid file response status")
                self._send_file(response)
                return
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
        finally:
            if uploaded_file is not None:
                uploaded_file.cleanup()
            if upload_lock_acquired:
                self.local_server.upload_lock.release()

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
        upload_root: Path | None = None,
    ) -> None:
        self.host = validate_bind_host(host)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.application = application or _not_found
        self.session_token = secrets.token_urlsafe(32)
        self.write_lock = threading.Lock()
        self.upload_lock = threading.Lock()
        self.upload_root = upload_root or (
            Path(tempfile.gettempdir()) / "inno-collector-web-uploads"
        )
        if self.upload_root.exists() and self.upload_root.is_symlink():
            raise ValueError("upload root must not be a symlink")
        self.upload_root.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            "pid": _ready_process_id(),
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
    parser.add_argument("--support-root", type=Path)
    parser.add_argument("--projects", type=Path)
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    support_root = (
        arguments.support_root
        or Path(
            os.environ.get(
                "INNO_COLLECTOR_SUPPORT_ROOT",
                "~/Library/Application Support/com.inno.news.collector",
            )
        )
    ).expanduser()
    vault = support_root / "Runtime" / "vault" / "英诺被投项目资讯库"
    from .controller import WebController
    from .downloads import DownloadRegistry
    from .moore_runtime import MooreRuntime
    from .uploads import DraftUploadManager

    exporter_runtime = support_root / "ExporterRuntime"
    delivery_root = support_root / "DeliveryTemp"
    download_registry = DownloadRegistry(
        delivery_root,
        vault_root=vault,
        exporter_runtime_root=exporter_runtime,
    )

    server = LocalWebServer(
        host=arguments.host,
        port=arguments.port,
        application=WebController(
            vault,
            moore_runtime=MooreRuntime(exporter_runtime),
            projects_path=arguments.projects,
            runtime_dir=support_root / "Runtime",
            delivery_root=delivery_root,
            download_registry=download_registry,
            draft_upload_manager=DraftUploadManager(support_root / "DraftInbox"),
        ),
        upload_root=support_root / "UploadTemp",
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
