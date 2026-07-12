from __future__ import annotations

import re
import hashlib
from collections.abc import Callable
from pathlib import Path

from .. import __version__
from ..config import load_projects
from ..diagnostics import sanitize_diagnostic
from ..exporter import ExporterCommandError
from ..models import PipelineRunResult, ProjectAccount
from ..package import lint_vault
from ..pipeline import (
    CollectionPipeline,
    PipelineAuthenticationError,
    PipelineCancelledError,
    PipelineConfigurationError,
)
from .jobs import JobBusyError, JobCancelled, JobGoneError, JobManager, JobOutcome
from .responses import WebResponse


Linter = Callable[[Path], dict[str, object]]
BooleanProvider = Callable[[], bool]
JobProvider = Callable[[], dict[str, object] | None]
PreflightRunner = Callable[[tuple[ProjectAccount, ...], str], PipelineRunResult]
CollectionRunner = Callable[
    [
        tuple[ProjectAccount, ...],
        str,
        Callable[[dict[str, object]], None],
        Callable[[], bool],
    ],
    PipelineRunResult,
]
_ASSET_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8", True),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8", False),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8", False),
}
_MAX_ASSET_BYTES = 1 << 20
_LOGIN_ROUTE = re.compile(
    r"^/api/login/([0-9a-f]{32})/(qrcode|status|complete)$"
)
_JOB_ROUTE = re.compile(
    r"^/api/jobs/([A-Za-z0-9_-]{24,64})(?:/(events|cancel))?$"
)
_DEFAULT_MOORE_BASE_URL = "https://down.mptext.top"
_LOGIN_MESSAGES = {
    "waiting_for_scan": "请使用微信扫描二维码。",
    "scanned_waiting_confirm": "已扫码，请在微信中确认登录。",
    "confirmed": "微信已确认，正在完成本机登录。",
    "complete": "登录已完成，只在这台 Mac 上保存。",
    "expired": "二维码已过期，请重新生成。",
    "account_not_bound_email": "该微信账号未绑定可用的公众号后台账号。",
    "cancelled": "登录已取消，请重新开始。",
    "failed": "登录失败，请稍后重新尝试。",
    "unknown": "暂时无法确认登录状态，请稍后重试。",
}


def _not_found() -> tuple[int, dict]:
    return 404, {
        "ok": False,
        "error": {"code": "not_found", "message": "Not found."},
    }


class WebController:
    def __init__(
        self,
        vault: Path,
        *,
        authenticated: BooleanProvider | None = None,
        recent_job: JobProvider | None = None,
        linter: Linter = lint_vault,
        assets_root: Path | None = None,
        moore_runtime: object | None = None,
        projects_path: Path | None = None,
        runtime_dir: Path | None = None,
        preflight_runner: PreflightRunner | None = None,
        collection_runner: CollectionRunner | None = None,
        job_manager: JobManager | None = None,
    ) -> None:
        self.vault = Path(vault)
        self.moore_runtime = moore_runtime
        if authenticated is not None:
            self._authenticated = authenticated
        elif moore_runtime is not None:
            self._authenticated = self._runtime_authenticated
        else:
            self._authenticated = lambda: False
        self._recent_job = recent_job or (lambda: None)
        self._linter = linter
        self.assets_root = assets_root or Path(__file__).with_name("assets")
        self.projects_path = projects_path or (
            Path(__file__).with_name("resources") / "projects.json"
        )
        self.runtime_dir = runtime_dir or self.vault.parents[1]
        self._preflight_runner = preflight_runner
        self._collection_runner = collection_runner
        self.job_manager = job_manager or JobManager()
        self._successful_preflight_hash: str | None = None

    def _runtime_authenticated(self) -> bool:
        if self.moore_runtime is None:
            return False
        payload = self.moore_runtime.auth_check()
        return (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("status") == "valid"
        )

    def _safe_recent_job(self) -> dict | None:
        try:
            raw = self._recent_job()
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        safe: dict = {}
        for field in (
            "id",
            "status",
            "stage",
            "summary",
            "started_at",
            "finished_at",
        ):
            value = raw.get(field)
            if type(value) is int:
                safe[field] = value
            elif isinstance(value, str):
                safe[field] = sanitize_diagnostic(value, fallback="")
        return safe or None

    def _bootstrap(self) -> tuple[int, dict]:
        try:
            authenticated = self._authenticated() is True
        except Exception:
            authenticated = False
        return 200, {
            "version": __version__,
            "authenticated": authenticated,
            "recent_job": self._safe_recent_job(),
            "capabilities": (
                ["read_library", "login", "preflight", "collection"]
                if (
                    self.moore_runtime is not None
                    or self._preflight_runner is not None
                    or self._collection_runner is not None
                )
                else ["read_library"]
            ),
        }

    def _login_error(self, *, unavailable: bool = False) -> tuple[int, dict]:
        return (503 if unavailable else 409), {
            "ok": False,
            "error": {
                "code": "login_service_unavailable" if unavailable else "login_unavailable",
                "message": (
                    "本机登录服务暂时不可用，请稍后重试。"
                    if unavailable
                    else "当前登录会话不可用，请重新开始登录。"
                ),
            },
        }

    def _start_login(self) -> tuple[int, dict]:
        if self.moore_runtime is None:
            return self._login_error(unavailable=True)
        try:
            payload = self.moore_runtime.start_login(_DEFAULT_MOORE_BASE_URL)
        except (ExporterCommandError, OSError, RuntimeError):
            return self._login_error(unavailable=True)
        if not isinstance(payload, dict):
            return self._login_error(unavailable=True)
        safe = {
            field: payload[field]
            for field in ("login_id", "expires_at", "qrcode_content_type")
            if field in payload
        }
        return 200, safe

    def _login_action(
        self,
        login_id: str,
        action: str,
        payload: object,
    ) -> tuple[int, object] | WebResponse:
        if self.moore_runtime is None:
            return self._login_error(unavailable=True)
        try:
            if action == "qrcode":
                body, content_type = self.moore_runtime.read_qrcode(login_id)
                if not isinstance(body, bytes) or content_type not in {
                    "image/png",
                    "image/jpeg",
                    "image/gif",
                    "image/webp",
                }:
                    return self._login_error()
                return WebResponse(200, body, content_type)
            if action == "status":
                result = self.moore_runtime.login_status(login_id)
                if not isinstance(result, dict):
                    return self._login_error()
                status = str(result.get("status") or "unknown")
                return 200, {
                    field: result[field]
                    for field in (
                        "login_id",
                        "status",
                        "status_code",
                        "acct_size",
                        "ready_to_complete",
                    )
                    if field in result
                } | {"message_zh": _LOGIN_MESSAGES.get(status, _LOGIN_MESSAGES["unknown"])}
            if action == "complete":
                profile = ""
                if isinstance(payload, dict) and isinstance(payload.get("profile"), str):
                    profile = payload["profile"][:80]
                result = self.moore_runtime.complete_login(login_id, profile)
                if not isinstance(result, dict):
                    return self._login_error()
                allowed = {"profile_id", "display_name", "expires_at", "nickname", "avatar"}
                return 200, {key: value for key, value in result.items() if key in allowed}
        except (ExporterCommandError, OSError, RuntimeError):
            return self._login_error()
        return _not_found()

    def _run_preflight(
        self,
        projects: tuple[ProjectAccount, ...],
        since: str,
    ) -> PipelineRunResult:
        if self._preflight_runner is not None:
            return self._preflight_runner(projects, since)
        if self.moore_runtime is None:
            raise PipelineAuthenticationError("local login is unavailable")
        return CollectionPipeline(
            self.moore_runtime,
            runtime_dir=self.runtime_dir,
        ).run(projects, since=since, dry_run=True)

    def _preflight_body(self, payload: object) -> tuple[int, dict]:
        if not isinstance(payload, dict):
            return 400, {
                "ok": False,
                "error": {"code": "invalid_request", "message": "请求格式不正确。"},
            }
        since = payload.get("since", "2026-01-01")
        if since != "2026-01-01":
            return 400, {
                "ok": False,
                "error": {
                    "code": "invalid_date_filter",
                    "message": "当前采集范围固定从 2026-01-01 开始。",
                },
            }
        try:
            projects = load_projects(self.projects_path)
        except (OSError, UnicodeError, ValueError):
            return 500, {
                "ok": False,
                "error": {"code": "invalid_projects", "message": "项目映射资源不可用。"},
            }
        try:
            result = self._run_preflight(projects, since)
        except PipelineAuthenticationError:
            rows = [
                {
                    "project": project.project,
                    "account": project.account,
                    "mapping": "not_checked",
                    "login": "invalid",
                    "catalog": 0,
                    "date_filter": since,
                    "status": "failed",
                    "reason": "本机登录已失效，请重新扫码登录。",
                }
                for project in projects
            ]
            return 200, {"ok": False, "projects": rows, "failed_projects": len(rows)}
        except (PipelineConfigurationError, OSError, RuntimeError):
            rows = [
                {
                    "project": project.project,
                    "account": project.account,
                    "mapping": "not_checked",
                    "login": "unknown",
                    "catalog": 0,
                    "date_filter": since,
                    "status": "failed",
                    "reason": "预检暂时无法完成，请稍后重试。",
                }
                for project in projects
            ]
            return 200, {"ok": False, "projects": rows, "failed_projects": len(rows)}

        rows = []
        for project_result in result.projects:
            reason = sanitize_diagnostic(project_result.error, fallback="")
            rows.append(
                {
                    "project": project_result.project,
                    "account": project_result.account,
                    "mapping": "failed" if reason.startswith("resolve:") else "matched",
                    "login": "valid",
                    "catalog": project_result.discovered,
                    "date_filter": since,
                    "status": project_result.status,
                    "reason": reason,
                }
            )
        return 200, {
            "ok": result.failed_projects == 0,
            "projects": rows,
            "failed_projects": result.failed_projects,
        }

    def _config_digest(self) -> str:
        if self.projects_path.is_symlink() or not self.projects_path.is_file():
            raise OSError("projects resource is unavailable")
        return "sha256:" + hashlib.sha256(self.projects_path.read_bytes()).hexdigest()

    def _preflight(self, payload: object) -> tuple[int, dict]:
        if not isinstance(payload, dict) or payload.get("since", "2026-01-01") != "2026-01-01":
            return self._preflight_body(payload)

        def operation(_context):
            status, body = self._preflight_body(payload)
            return {"http_status": status, "payload": body}

        try:
            job_id = self.job_manager.submit("preflight", operation)
            snapshot = self.job_manager.wait(job_id)
        except JobBusyError:
            return 409, {
                "ok": False,
                "error": {"code": "job_busy", "message": "已有任务正在运行，请稍后再试。"},
            }
        result = snapshot.get("result")
        if not isinstance(result, dict):
            return 500, {
                "ok": False,
                "error": {"code": "preflight_failed", "message": "预检任务执行失败。"},
            }
        status = result.get("http_status")
        body = result.get("payload")
        if type(status) is not int or not isinstance(body, dict):
            return 500, {
                "ok": False,
                "error": {"code": "preflight_failed", "message": "预检任务执行失败。"},
            }
        if status == 200 and body.get("ok") is True:
            try:
                self._successful_preflight_hash = self._config_digest()
            except OSError:
                self._successful_preflight_hash = None
        return status, body

    def _run_collection(
        self,
        projects: tuple[ProjectAccount, ...],
        since: str,
        progress: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
    ) -> PipelineRunResult:
        if self._collection_runner is not None:
            return self._collection_runner(projects, since, progress, cancelled)
        if self.moore_runtime is None:
            raise PipelineAuthenticationError("local login is unavailable")
        return CollectionPipeline(
            self.moore_runtime,
            runtime_dir=self.runtime_dir,
        ).run(
            projects,
            since=since,
            dry_run=False,
            progress=progress,
            cancelled=cancelled,
        )

    def _start_collection(self, payload: object) -> tuple[int, dict]:
        if not isinstance(payload, dict) or payload.get("since", "2026-01-01") != "2026-01-01":
            return 400, {
                "ok": False,
                "error": {"code": "invalid_date_filter", "message": "当前采集范围固定从 2026-01-01 开始。"},
            }
        try:
            current_hash = self._config_digest()
        except OSError:
            current_hash = ""
        if not current_hash or current_hash != self._successful_preflight_hash:
            return 409, {
                "ok": False,
                "error": {"code": "preflight_required", "message": "请先运行并通过当前项目配置的预检。"},
            }
        try:
            projects = load_projects(self.projects_path)
        except (OSError, UnicodeError, ValueError):
            return 500, {
                "ok": False,
                "error": {"code": "invalid_projects", "message": "项目映射资源不可用。"},
            }

        def operation(context):
            def on_progress(event: dict[str, object]) -> None:
                context.emit(
                    str(event.get("type") or ""),
                    project=str(event.get("project") or ""),
                    stage=str(event.get("stage") or ""),
                    counts=(event.get("counts") if isinstance(event.get("counts"), dict) else {}),
                )

            try:
                result = self._run_collection(
                    projects,
                    "2026-01-01",
                    on_progress,
                    context.is_cancelled,
                )
            except PipelineCancelledError:
                raise JobCancelled from None
            summary = {
                "project_count": result.project_count,
                "failed_projects": result.failed_projects,
                "article_count": result.article_count,
                "duplicate_count": result.duplicate_count,
            }
            return JobOutcome(
                "partial" if result.failed_projects else "succeeded",
                summary,
            )

        try:
            job_id = self.job_manager.submit("collection", operation)
        except JobBusyError:
            return 409, {
                "ok": False,
                "error": {"code": "job_busy", "message": "已有任务正在运行，请稍后再试。"},
            }
        return 202, {"ok": True, "job_id": job_id}

    def _job_action(self, job_id: str, action: str | None) -> tuple[int, dict]:
        try:
            if action == "events":
                return 200, self.job_manager.events(job_id)
            if action == "cancel":
                return 200, self.job_manager.cancel(job_id)
            return 200, self.job_manager.get(job_id)
        except JobGoneError:
            return 410, {
                "ok": False,
                "error": {"code": "job_gone", "message": "该任务已结束并从本机记录中清理。"},
            }

    def _library_summary(self) -> tuple[int, dict]:
        if not self.vault.exists() or not self.vault.is_dir():
            return 200, {
                "exists": False,
                "healthy": True,
                "article_count": 0,
                "project_count": 0,
                "failed_projects": 0,
                "issue_count": 0,
            }
        report = self._linter(self.vault)
        errors = report.get("errors")
        issue_count = len(errors) if isinstance(errors, list) else 0

        def safe_count(field: str) -> int:
            value = report.get(field)
            return value if type(value) is int and value >= 0 else 0

        return 200, {
            "exists": True,
            "healthy": issue_count == 0,
            "article_count": safe_count("article_count"),
            "project_count": safe_count("project_count"),
            "failed_projects": safe_count("failed_projects"),
            "issue_count": issue_count,
        }

    def _asset(self, route: str) -> WebResponse | tuple[int, dict]:
        descriptor = _ASSET_ROUTES.get(route)
        if descriptor is None:
            return _not_found()
        filename, content_type, inject_token = descriptor
        root = self.assets_root
        if root.is_symlink():
            return _not_found()
        path = root / filename
        if path.is_symlink():
            return _not_found()
        try:
            resolved_root = root.resolve(strict=True)
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
            if not resolved.is_file() or resolved.stat().st_size > _MAX_ASSET_BYTES:
                return _not_found()
            body = resolved.read_bytes()
        except (OSError, ValueError):
            return _not_found()
        return WebResponse(
            status=200,
            body=body,
            content_type=content_type,
            inject_session_token=inject_token,
        )

    def __call__(
        self,
        method: str,
        path: str,
        _payload: object,
    ) -> tuple[int, object] | WebResponse:
        if method not in {"GET", "HEAD", "POST"}:
            return 405, {
                "ok": False,
                "error": {"code": "method_not_allowed", "message": "Method not allowed."},
            }
        if path == "/api/bootstrap" and method in {"GET", "HEAD"}:
            return self._bootstrap()
        if path == "/api/library/summary" and method in {"GET", "HEAD"}:
            return self._library_summary()
        if path == "/api/login/start" and method == "POST":
            return self._start_login()
        if path == "/api/preflight" and method == "POST":
            return self._preflight(_payload)
        if path == "/api/collection" and method == "POST":
            return self._start_collection(_payload)
        job_match = _JOB_ROUTE.fullmatch(path)
        if job_match is not None:
            job_id, action = job_match.groups()
            expected_method = "POST" if action == "cancel" else "GET"
            if method != expected_method:
                return 405, {
                    "ok": False,
                    "error": {"code": "method_not_allowed", "message": "Method not allowed."},
                }
            return self._job_action(job_id, action)
        login_match = _LOGIN_ROUTE.fullmatch(path)
        if login_match is not None:
            login_id, action = login_match.groups()
            expected_method = "POST" if action == "complete" else "GET"
            if method != expected_method:
                return 405, {
                    "ok": False,
                    "error": {"code": "method_not_allowed", "message": "Method not allowed."},
                }
            return self._login_action(login_id, action, _payload)
        if path in _ASSET_ROUTES and method in {"GET", "HEAD"}:
            return self._asset(path)
        return _not_found()
