from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from types import ModuleType
from typing import Protocol
from urllib.parse import urlsplit

from ..diagnostics import sanitize_diagnostic
from ..exporter import (
    ExporterCommandError,
    validate_download_payload,
    validate_object_rows,
    resolve_exact_account,
    validate_success_payload,
    validate_sync_payload,
)
from ..models import ProjectAccount


MAX_QRCODE_BYTES = 2 << 20
DISCOVERY_PAGE_SIZE = 10
MAX_DISCOVERY_PAGES = 5
MAX_DISCOVERY_CANDIDATES = DISCOVERY_PAGE_SIZE * MAX_DISCOVERY_PAGES
MAX_DISCOVERY_QUERIES = 8
MAX_ACCOUNT_NAME_LENGTH = 200
MAX_FAKEID_LENGTH = 512
MAX_AVATAR_URL_LENGTH = 2048
MAX_DESCRIPTION_LENGTH = 4096
AUTH_CACHE_TTL_SECONDS = 5.0
AUTH_RETRY_DELAY_SECONDS = 0.05
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
    def search_accounts(
        self,
        base: Path,
        keyword: str,
        begin: int = 0,
        size: int = DISCOVERY_PAGE_SIZE,
        profile: str = "",
    ) -> dict: ...
    def upsert_account(self, base: Path, account: dict) -> dict: ...
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
        "search_accounts",
        "upsert_account",
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


def _has_unsafe_control(value: str, *, allow_newlines: bool = False) -> bool:
    allowed = {"\t", "\n", "\r"} if allow_newlines else set()
    return any(
        character not in allowed
        and (ord(character) < 32 or 127 <= ord(character) < 160)
        for character in value
    )


def _safe_text(
    value: object,
    *,
    maximum: int,
    allow_empty: bool,
    allow_newlines: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ExporterCommandError("exporter returned invalid account search")
    result = value.strip()
    try:
        encoded_length = len(result.encode("utf-8"))
    except UnicodeEncodeError:
        raise ExporterCommandError("exporter returned invalid account search") from None
    if (not result and not allow_empty) or encoded_length > maximum:
        raise ExporterCommandError("exporter returned invalid account search")
    if _has_unsafe_control(result, allow_newlines=allow_newlines):
        raise ExporterCommandError("exporter returned invalid account search")
    return result


def _safe_account_name(value: object) -> str:
    result = _safe_text(
        value,
        maximum=MAX_ACCOUNT_NAME_LENGTH,
        allow_empty=False,
    )
    if result in {".", ".."} or "/" in result or "\\" in result:
        raise ExporterCommandError("exporter returned unsafe account name")
    return result


def _safe_avatar_url(value: object) -> str:
    result = _safe_text(
        value,
        maximum=MAX_AVATAR_URL_LENGTH,
        allow_empty=True,
    )
    if not result:
        return ""
    parsed = urlsplit(result)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ExporterCommandError("exporter returned invalid account search")
    return result


def _safe_remote_account(row: dict) -> dict:
    fakeid = _safe_text(
        row.get("fakeid"),
        maximum=MAX_FAKEID_LENGTH,
        allow_empty=False,
    )
    nickname = _safe_account_name(row.get("nickname"))
    alias = _safe_text(
        row.get("alias", ""),
        maximum=MAX_ACCOUNT_NAME_LENGTH,
        allow_empty=True,
    )
    if "/" in alias or "\\" in alias:
        raise ExporterCommandError("exporter returned invalid account search")
    avatar_url = _safe_avatar_url(row.get("avatar_url", ""))
    description = _safe_text(
        row.get("description", ""),
        maximum=MAX_DESCRIPTION_LENGTH,
        allow_empty=True,
        allow_newlines=True,
    )
    article_count = row.get("article_count", 0)
    if type(article_count) is not int or not 0 <= article_count <= 1_000_000_000:
        raise ExporterCommandError("exporter returned invalid account search")
    return {
        "fakeid": fakeid,
        "nickname": nickname,
        "alias": alias,
        "avatar_url": avatar_url,
        "description": description,
        "article_count": article_count,
    }


def _merge_candidate(target: dict[str, dict], candidate: dict) -> bool:
    fakeid = candidate["fakeid"]
    current = target.get(fakeid)
    if current is None:
        target[fakeid] = candidate
        return True
    if (
        current.get("nickname") != candidate.get("nickname")
        or current.get("alias") != candidate.get("alias")
    ):
        raise ExporterCommandError("exporter returned conflicting account search")
    return False


def _unique_identifiers(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return tuple(result)


def _resolution_tiers(
    project: ProjectAccount,
) -> tuple[tuple[ProjectAccount, tuple[str, ...]], ...]:
    tiers: list[tuple[ProjectAccount, tuple[str, ...]]] = []
    # The primary account name in the immutable project mapping is the
    # authoritative lookup.  A configured WeChat ID is a strict fallback for
    # renamed/missing primary results; aliases are historical fallbacks only.
    # Each tier still fails closed if it resolves to more than one fakeid.
    account = project.account.strip()
    if account:
        tiers.append(
            (
                ProjectAccount(project=project.project, account=account),
                (account,),
            )
        )
    wechat_id = project.wechat_id.strip()
    if wechat_id:
        tiers.append(
            (
                ProjectAccount(
                    project=project.project,
                    account="",
                    wechat_id=wechat_id,
                ),
                (wechat_id,),
            )
        )
    aliases = _unique_identifiers(project.aliases)
    if aliases:
        tiers.append(
            (
                ProjectAccount(
                    project=project.project,
                    account="",
                    aliases=aliases,
                ),
                aliases,
            )
        )
    return tuple(tiers)


def _resolve_tier(project: ProjectAccount, rows: list[dict]) -> dict | None:
    matches: list[dict] = []
    for row in rows:
        try:
            matches.append(resolve_exact_account(project, [row]))
        except ExporterCommandError:
            continue
    if not matches:
        return None
    return resolve_exact_account(project, rows)


def _resolve_priority(project: ProjectAccount, rows: list[dict]) -> dict | None:
    for tier, _queries in _resolution_tiers(project):
        match = _resolve_tier(tier, rows)
        if match is not None:
            return match
    return None


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
        self._functions = functions
        # The local HTTP server handles independent browser requests on
        # separate threads, while the bundled exporter shares one SQLite
        # runtime and login profile.  Keep upstream calls single-file so a
        # page bootstrap cannot race a preflight or login operation.
        self._operation_lock = RLock()
        self._auth_cache: dict[str, tuple[float, dict]] = {}
        self._login_sessions: dict[str, _LoginSession] = {}
        self._discovery_errors: dict[str, str] = {}

    @property
    def functions(self) -> MooreFunctions:
        if self._functions is None:
            self._functions = load_moore_functions()
        return self._functions

    def _call(self, operation, *arguments):
        with self._operation_lock:
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
        with self._operation_lock:
            # Never let a previously cached valid result survive a completed
            # (or malformed) login transition.
            self._auth_cache.clear()
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
        with self._operation_lock:
            now = monotonic()
            cached = self._auth_cache.get(profile)
            if cached is not None and now < cached[0]:
                return dict(cached[1])
            self._auth_cache.pop(profile, None)

            payload = self._call(self.functions.auth_check, profile)
            if (
                isinstance(payload, dict)
                and payload.get("ok") is False
                and payload.get("status") == "error"
            ):
                # The upstream adapter uses this exact status for temporary
                # network/SQLite failures. Retry it once, but never retry an
                # expired login or a malformed response.
                sleep(AUTH_RETRY_DELAY_SECONDS)
                payload = self._call(self.functions.auth_check, profile)

            payload = validate_success_payload(payload)
            safe: dict = {"ok": True}
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
            if safe.get("status") == "valid":
                self._auth_cache[profile] = (
                    now + AUTH_CACHE_TTL_SECONDS,
                    dict(safe),
                )
            return safe

    def _search_page(self, keyword: str, begin: int) -> list[dict]:
        payload = self._call(
            self.functions.search_accounts,
            keyword,
            begin,
            DISCOVERY_PAGE_SIZE,
            "",
        )
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ExporterCommandError("exporter returned invalid account search")
        raw_code = payload.get("raw_code")
        if raw_code is not None and not (
            (type(raw_code) is int and raw_code == 0)
            or (isinstance(raw_code, str) and raw_code == "0")
        ):
            raise ExporterCommandError("exporter returned invalid account search")
        rows = payload.get("accounts")
        count = payload.get("count")
        if (
            payload.get("keyword") != keyword
            or type(payload.get("begin")) is not int
            or payload["begin"] != begin
            or type(payload.get("size")) is not int
            or payload["size"] != DISCOVERY_PAGE_SIZE
            or type(count) is not int
            or count < 0
            or not isinstance(rows, list)
            or len(rows) > DISCOVERY_PAGE_SIZE
            or count != len(rows)
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ExporterCommandError("exporter returned invalid account search")
        return [_safe_remote_account(row) for row in rows]

    def _search_query(self, keyword: str) -> list[dict]:
        safe_keyword = _safe_text(
            keyword,
            maximum=MAX_ACCOUNT_NAME_LENGTH,
            allow_empty=False,
        )
        candidates: dict[str, dict] = {}
        for page in range(MAX_DISCOVERY_PAGES + 1):
            begin = page * DISCOVERY_PAGE_SIZE
            rows = self._search_page(safe_keyword, begin)
            if not rows:
                return list(candidates.values())
            if page == MAX_DISCOVERY_PAGES:
                raise ExporterCommandError("exporter account search exceeded safe limit")
            added = 0
            for row in rows:
                if _merge_candidate(candidates, row):
                    added += 1
            if not added or len(candidates) > MAX_DISCOVERY_CANDIDATES:
                raise ExporterCommandError("exporter returned invalid account pagination")
        raise ExporterCommandError("exporter account search exceeded safe limit")

    def _discover_exact(self, project: ProjectAccount) -> dict | None:
        candidates: dict[str, dict] = {}
        searched: set[str] = set()
        for tier, queries in _resolution_tiers(project):
            for query in queries:
                query_key = query.casefold()
                if query_key in searched:
                    continue
                if len(searched) >= MAX_DISCOVERY_QUERIES:
                    raise ExporterCommandError(
                        "exporter account search exceeded safe limit"
                    )
                searched.add(query_key)
                for candidate in self._search_query(query):
                    _merge_candidate(candidates, candidate)
                    if len(candidates) > MAX_DISCOVERY_CANDIDATES:
                        raise ExporterCommandError(
                            "exporter account search exceeded safe limit"
                        )
            match = _resolve_tier(tier, list(candidates.values()))
            if match is not None:
                return match
        return None

    def _upsert_remote_account(self, candidate: dict) -> dict:
        payload = self._call(self.functions.upsert_account, candidate)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ExporterCommandError("exporter returned invalid account upsert")
        account = payload.get("account")
        if not isinstance(account, dict):
            raise ExporterCommandError("exporter returned invalid account upsert")
        account_id = account.get("id")
        if (
            type(account_id) is not int
            or account_id <= 0
            or account.get("fakeid") != candidate["fakeid"]
        ):
            raise ExporterCommandError("exporter returned invalid account upsert")
        return {
            "id": account_id,
            "nickname": candidate["nickname"],
            "alias": candidate["alias"],
        }

    def _validated_cached_account(self, row: dict) -> dict:
        account_id = row.get("id")
        if type(account_id) is not int or account_id <= 0:
            raise ExporterCommandError("exporter returned invalid cached account")
        _safe_account_name(row.get("nickname"))
        alias = row.get("alias", "")
        _safe_text(
            alias,
            maximum=MAX_ACCOUNT_NAME_LENGTH,
            allow_empty=True,
        )
        if isinstance(alias, str) and ("/" in alias or "\\" in alias):
            raise ExporterCommandError("exporter returned invalid cached account")
        return row

    def ensure_exact_accounts(
        self,
        projects: Iterable[ProjectAccount],
    ) -> list[dict]:
        """Explicitly discover missing exact accounts before a pure pipeline run."""
        self._discovery_errors = {}
        try:
            cached = list(self.accounts())
        except Exception:
            raise ExporterCommandError("local account cache is unavailable") from None

        for project in projects:
            project_key = project.project
            try:
                existing = _resolve_priority(project, cached)
                if existing is not None:
                    self._validated_cached_account(existing)
                    continue
            except Exception:
                self._discovery_errors[project_key] = "account discovery failed"
                continue

            try:
                candidate = self._discover_exact(project)
                if candidate is None:
                    raise ExporterCommandError("account discovery found no exact match")
                cached.append(self._upsert_remote_account(candidate))
            except Exception:
                self._discovery_errors[project_key] = "account discovery failed"
        return cached

    def resolve_exact(self, project: ProjectAccount, rows: list[dict]) -> dict:
        safe_rows = validate_object_rows(rows, "accounts")
        match = _resolve_priority(project, safe_rows)
        if match is not None:
            return self._validated_cached_account(match)
        discovery_error = self._discovery_errors.get(project.project)
        if discovery_error:
            raise ExporterCommandError(discovery_error)
        return resolve_exact_account(project, safe_rows)

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
