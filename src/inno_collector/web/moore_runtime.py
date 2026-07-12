from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

from ..diagnostics import sanitize_diagnostic
from ..exporter import (
    ExporterCommandError,
    validate_download_payload,
    validate_object_rows,
    validate_success_payload,
    validate_sync_payload,
)


MAX_QRCODE_BYTES = 2 << 20
_LOGIN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_QRCODE_TYPES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


class MooreFunctions(Protocol):
    def start_qr_login(self, base: Path, base_url: str) -> dict: ...
    def qr_login_status(self, base: Path, login_id: str) -> dict: ...
    def complete_qr_login(
        self, base: Path, login_id: str, profile: str = ""
    ) -> dict: ...
    def auth_check(self, base: Path, profile: str = "") -> dict: ...
    def list_accounts(self, base: Path) -> list[dict]: ...
    def sync_account_articles(
        self,
        base: Path,
        account_id: int,
        limit: int,
        keyword: str = "",
        profile: str = "",
    ) -> dict: ...
    def list_articles(
        self,
        base: Path,
        account_id: int = 0,
        limit: int = 100,
        keyword: str = "",
        collection_id: int = 0,
        downloaded: str = "",
    ) -> list[dict]: ...
    def download_articles(
        self,
        base: Path,
        article_ids: list[int],
        output_dir: str = "",
        no_assets: bool = False,
        account_nickname: str = "",
    ) -> dict: ...


def load_moore_functions() -> ModuleType:
    """Import the bundled Moore runtime only for the real application process."""
    try:
        module = importlib.import_module("wechat_exporter")
    except (ImportError, OSError) as exc:
        raise ExporterCommandError("local exporter runtime is unavailable") from exc

    required = (
        "start_qr_login",
        "qr_login_status",
        "complete_qr_login",
        "auth_check",
        "list_accounts",
        "sync_account_articles",
        "list_articles",
        "download_articles",
    )
    if any(not callable(getattr(module, name, None)) for name in required):
        raise ExporterCommandError("local exporter runtime is incompatible")
    return module


@dataclass(frozen=True, slots=True)
class _LoginSession:
    qrcode_path: Path
    content_type: str
    expires_at: str


class MooreRuntime:
    """Safe, in-process boundary around the Moore exporter functions."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        functions: MooreFunctions | None = None,
    ) -> None:
        candidate = runtime_dir.expanduser()
        if candidate.exists() and candidate.is_symlink():
            raise ValueError("runtime directory must not be a symlink")
        candidate.mkdir(parents=True, exist_ok=True)
        self._runtime_input = candidate.absolute()
        self.runtime_dir = candidate.resolve(strict=True)
        self.functions = functions if functions is not None else load_moore_functions()
        self._login_sessions: dict[str, _LoginSession] = {}

    def _call(self, operation, *arguments):
        try:
            return operation(self.runtime_dir, *arguments)
        except ExporterCommandError:
            raise
        except Exception:
            raise ExporterCommandError("local exporter operation failed") from None

    def _registered_session(self, login_id: str) -> _LoginSession:
        if not isinstance(login_id, str) or not _LOGIN_ID_RE.fullmatch(login_id):
            raise ExporterCommandError("login session is unavailable")
        session = self._login_sessions.get(login_id)
        if session is None:
            raise ExporterCommandError("login session is unavailable")
        return session

    def _safe_qrcode(self, raw_path: object) -> tuple[Path, str]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ExporterCommandError("exporter returned invalid QR code file")
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            raise ExporterCommandError("exporter returned invalid QR code file")
        absolute_path = path.absolute()
        for base in (self._runtime_input, self.runtime_dir):
            try:
                relative = absolute_path.relative_to(base)
            except ValueError:
                continue
            current = base
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise ExporterCommandError("exporter returned invalid QR code file")
            break
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.runtime_dir)
        except (OSError, ValueError):
            raise ExporterCommandError("exporter returned invalid QR code file") from None
        if not resolved.is_file():
            raise ExporterCommandError("exporter returned invalid QR code file")
        try:
            size = resolved.stat().st_size
            with resolved.open("rb") as qrcode_file:
                header = qrcode_file.read(12)
        except OSError:
            raise ExporterCommandError("exporter returned invalid QR code file") from None
        if size <= 0 or size > MAX_QRCODE_BYTES:
            raise ExporterCommandError("exporter returned invalid QR code file")
        content_type = next(
            (kind for signature, kind in _QRCODE_TYPES.items() if header.startswith(signature)),
            "",
        )
        if not content_type and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            content_type = "image/webp"
        if not content_type:
            raise ExporterCommandError("exporter returned invalid QR code file")
        return resolved, content_type

    def start_login(self, base_url: str) -> dict:
        payload = self._call(self.functions.start_qr_login, base_url)
        payload = validate_success_payload(payload)
        login_id = payload.get("login_id")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(login_id, str)
            or not _LOGIN_ID_RE.fullmatch(login_id)
            or not isinstance(expires_at, str)
            or not expires_at.strip()
        ):
            raise ExporterCommandError("exporter returned invalid login session")
        qrcode_path, content_type = self._safe_qrcode(payload.get("qrcode_path"))
        self._login_sessions[login_id] = _LoginSession(
            qrcode_path=qrcode_path,
            content_type=content_type,
            expires_at=expires_at,
        )
        return {
            "login_id": login_id,
            "expires_at": expires_at,
            "qrcode_content_type": content_type,
        }

    def read_qrcode(self, login_id: str) -> tuple[bytes, str]:
        session = self._registered_session(login_id)
        qrcode_path, content_type = self._safe_qrcode(str(session.qrcode_path))
        try:
            return qrcode_path.read_bytes(), content_type
        except OSError:
            raise ExporterCommandError("login QR code is unavailable") from None

    def login_status(self, login_id: str) -> dict:
        self._registered_session(login_id)
        payload = self._call(self.functions.qr_login_status, login_id)
        payload = validate_success_payload(payload)
        if payload.get("login_id") != login_id:
            raise ExporterCommandError("exporter returned invalid login status")
        status = payload.get("status")
        status_code = payload.get("status_code")
        ready = payload.get("ready_to_complete")
        acct_size = payload.get("acct_size")
        if (
            not isinstance(status, str)
            or type(status_code) is not int
            or type(ready) is not bool
            or (acct_size is not None and type(acct_size) is not int)
        ):
            raise ExporterCommandError("exporter returned invalid login status")
        message = sanitize_diagnostic(payload.get("message", ""), fallback="")
        return {
            "login_id": login_id,
            "status": status,
            "status_code": status_code,
            "acct_size": acct_size,
            "message": message,
            "ready_to_complete": ready,
        }

    def complete_login(self, login_id: str, profile: str = "") -> dict:
        session = self._registered_session(login_id)
        payload = self._call(self.functions.complete_qr_login, login_id, profile)
        if not isinstance(payload, dict):
            raise ExporterCommandError("exporter returned invalid login completion")
        safe: dict = {}
        field_types = {
            "profile_id": int,
            "display_name": str,
            "expires_at": str,
            "nickname": str,
            "avatar": str,
        }
        for field, expected_type in field_types.items():
            value = payload.get(field)
            if value is None:
                continue
            if expected_type is int:
                if type(value) is not int:
                    raise ExporterCommandError("exporter returned invalid login completion")
            elif not isinstance(value, expected_type):
                raise ExporterCommandError("exporter returned invalid login completion")
            safe[field] = (
                sanitize_diagnostic(value, fallback="")
                if field == "avatar"
                else value
            )
        if type(safe.get("profile_id")) is not int:
            raise ExporterCommandError("exporter returned invalid login completion")
        self._login_sessions.pop(login_id, None)
        try:
            session.qrcode_path.unlink(missing_ok=True)
        except OSError:
            pass
        return safe

    def auth_check(self, profile: str = "") -> dict:
        payload = self._call(self.functions.auth_check, profile)
        payload = validate_success_payload(payload)
        safe: dict = {}
        for field in ("status", "profile", "expires_at"):
            if field in payload:
                if not isinstance(payload[field], str):
                    raise ExporterCommandError("exporter returned invalid auth status")
                safe[field] = payload[field]
        if "code" in payload:
            code = payload["code"]
            if code is not None and type(code) not in {int, str}:
                raise ExporterCommandError("exporter returned invalid auth status")
            safe["code"] = code
        return safe

    def accounts(self) -> list[dict]:
        rows = self._call(self.functions.list_accounts)
        return validate_object_rows(rows, "accounts")

    def sync(self, account_id: int, limit: int = 1000) -> dict:
        payload = self._call(
            self.functions.sync_account_articles,
            account_id,
            limit,
            "",
            "",
        )
        return validate_sync_payload(payload, account_id)

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
        rows = self._call(
            self.functions.list_articles,
            account_id,
            limit,
            "",
            0,
            "",
        )
        return validate_object_rows(rows, "articles")

    def download(self, article_ids: list[int], output_root: Path) -> dict:
        payload = self._call(
            self.functions.download_articles,
            article_ids,
            str(output_root),
            False,
            "",
        )
        return validate_download_payload(payload)
