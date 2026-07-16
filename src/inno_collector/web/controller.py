from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..config import load_projects
from ..diagnostics import sanitize_diagnostic
from ..exporter import ExporterCommandError
from ..models import PipelineRunResult, ProjectAccount
from ..package import build_delivery_zip, lint_vault
from ..update_package import build_update_package
from ..pipeline import (
    CollectionPipeline,
    PipelineAuthenticationError,
    PipelineCancelledError,
    PipelineConfigurationError,
)
from .jobs import JobBusyError, JobCancelled, JobGoneError, JobManager, JobOutcome
from .downloads import DownloadGoneError, DownloadRegistry
from .requests import UploadedFile
from .responses import FileResponse, WebResponse
from .security import MAX_DOWNLOAD_BYTES
from .uploads import DraftUploadError


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
DeliveryBuilder = Callable[..., dict[str, object]]
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
_DELIVERY_ROUTE = re.compile(
    r"^/api/delivery/([A-Za-z0-9_-]{24,64})/download$"
)
_DRAFT_ACCEPT_ROUTE = re.compile(
    r"^/api/drafts/([A-Za-z0-9_-]{32,64})/accept$"
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
        delivery_root: Path | None = None,
        download_registry: DownloadRegistry | None = None,
        delivery_builder: DeliveryBuilder = build_update_package,
        customer_delivery_builder: DeliveryBuilder = build_delivery_zip,
        draft_upload_manager: object | None = None,
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
        self.delivery_root = Path(delivery_root) if delivery_root is not None else None
        self.download_registry = download_registry
        self._delivery_builder = delivery_builder
        self._customer_delivery_builder = customer_delivery_builder
        self.draft_upload_manager = draft_upload_manager

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
            "capabilities": self._capabilities(),
        }

    def _capabilities(self) -> list[str]:
        capabilities = ["read_library"]
        if (
            self.moore_runtime is not None
            or self._preflight_runner is not None
            or self._collection_runner is not None
        ):
            capabilities.extend(["login", "preflight", "collection"])
        if self.delivery_root is not None and self.download_registry is not None:
            capabilities.append("delivery")
        if self.draft_upload_manager is not None:
            capabilities.append("drafts")
        return capabilities

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
        discover_accounts = getattr(
            self.moore_runtime,
            "ensure_exact_accounts",
            None,
        )
        if callable(discover_accounts):
            auth = self.moore_runtime.auth_check()
            if (
                not isinstance(auth, dict)
                or auth.get("ok") is not True
                or auth.get("status") != "valid"
            ):
                raise PipelineAuthenticationError(
                    "exporter authentication is not valid"
                )
            discover_accounts(projects)
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

    def _stage_uploaded_base(self, uploaded: UploadedFile) -> Path:
        if (
            self.delivery_root is None
            or not uploaded.filename.casefold().endswith(".inno-update")
            or uploaded.size <= 0
            or uploaded.path.is_symlink()
        ):
            raise ValueError("invalid incremental base")
        root = self.delivery_root
        if root.exists() and root.is_symlink():
            raise ValueError("invalid delivery root")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = root / f".base-{secrets.token_urlsafe(18)}.inno-update"
        source_descriptor = destination_descriptor = -1
        try:
            source_descriptor = os.open(
                uploaded.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            source_stat = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_nlink != 1
                or source_stat.st_size != uploaded.size
            ):
                raise OSError
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            copied = 0
            while True:
                chunk = os.read(source_descriptor, 1 << 20)
                if not chunk:
                    break
                pending = memoryview(chunk)
                while pending:
                    written = os.write(destination_descriptor, pending)
                    if written <= 0:
                        raise OSError
                    pending = pending[written:]
                copied += len(chunk)
            if copied != uploaded.size:
                raise OSError
            os.fsync(destination_descriptor)
            with zipfile.ZipFile(destination) as archive:
                infos = archive.infolist()
                if (
                    not 1 <= len(infos) <= 4096
                    or sum(info.file_size for info in infos) > (1 << 30)
                    or any(
                        info.file_size > max(1, info.compress_size) * 1000
                        for info in infos
                    )
                ):
                    raise OSError
            return destination
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            destination.unlink(missing_ok=True)
            raise ValueError("invalid incremental base") from None
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if destination_descriptor >= 0:
                os.close(destination_descriptor)

    def _start_delivery(self, payload: object) -> tuple[int, dict]:
        if self.delivery_root is None or self.download_registry is None:
            return 503, {
                "ok": False,
                "error": {"code": "delivery_unavailable", "message": "交付服务暂时不可用。"},
            }
        base_package: Path | None = None
        created_at: str | None = None
        if isinstance(payload, UploadedFile):
            try:
                base_package = self._stage_uploaded_base(payload)
            except ValueError:
                return 400, {
                    "ok": False,
                    "error": {"code": "invalid_base_package", "message": "请选择有效的旧版 .inno-update。"},
                }
            kind = "incremental"
        elif isinstance(payload, dict):
            kind = payload.get("kind")
            created_at_value = payload.get("created_at")
            if created_at_value is not None:
                if not isinstance(created_at_value, str) or len(created_at_value) > 80:
                    return 400, {
                        "ok": False,
                        "error": {"code": "invalid_delivery", "message": "交付参数不正确。"},
                    }
                created_at = created_at_value
            if kind not in {"baseline", "customer"}:
                return 400, {
                    "ok": False,
                    "error": {"code": "base_upload_required", "message": "增量交付需要上传旧版 .inno-update。"},
                }
        else:
            return 400, {
                "ok": False,
                "error": {"code": "invalid_delivery", "message": "交付参数不正确。"},
            }

        suffix = ".zip" if kind == "customer" else ".inno-update"
        physical = self.delivery_root / f"delivery-{secrets.token_urlsafe(18)}{suffix}"
        customer_summary = physical.with_suffix(".summary.md")

        def operation(_context):
            registered_id: str | None = None
            try:
                if kind == "customer":
                    result = self._customer_delivery_builder(self.vault, physical)
                else:
                    result = self._delivery_builder(
                        self.vault,
                        physical,
                        base_package=base_package,
                        created_at=created_at,
                    )
                if not isinstance(result, dict):
                    raise ValueError("invalid delivery result")
                if kind == "customer":
                    counts = (
                        result.get("article_count"),
                        result.get("successful_projects"),
                        result.get("failed_projects"),
                    )
                    if any(type(value) is not int or value < 0 for value in counts):
                        raise ValueError("invalid delivery result")
                    if physical.stat().st_size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("delivery exceeded safe limit")
                    filename = (
                        "英诺客户资料库-"
                        f"{datetime.now().astimezone():%Y%m%d-%H%M}.zip"
                    )
                    record = self.download_registry.register(
                        physical,
                        filename=filename,
                        content_type="application/zip",
                    )
                    registered_id = record.id
                    return {
                        "kind": "customer",
                        "article_count": counts[0],
                        "successful_projects": counts[1],
                        "failed_projects": counts[2],
                        "package_sha256": record.sha256,
                        "download_id": record.id,
                        "filename": record.filename,
                        "size": record.size,
                    }
                safe_kind = result.get("kind")
                included = result.get("included")
                deleted = result.get("deleted")
                if (
                    safe_kind not in {"baseline", "incremental"}
                    or not isinstance(included, list)
                    or not isinstance(deleted, list)
                ):
                    raise ValueError("invalid delivery result")
                if physical.stat().st_size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("delivery exceeded safe limit")
                filename = f"英诺资讯-{safe_kind}-{secrets.token_hex(4)}.inno-update"
                record = self.download_registry.register(
                    physical,
                    filename=filename,
                    content_type="application/zip",
                )
                registered_id = record.id
                return {
                    "kind": safe_kind,
                    "base_version": result.get("base_version"),
                    "target_version": result.get("target_version"),
                    "included_count": len(included),
                    "deleted_count": len(deleted),
                    "package_sha256": record.sha256,
                    "download_id": record.id,
                    "filename": record.filename,
                    "size": record.size,
                }
            finally:
                if base_package is not None:
                    base_package.unlink(missing_ok=True)
                customer_summary.unlink(missing_ok=True)
                if registered_id is None:
                    physical.unlink(missing_ok=True)

        try:
            job_id = self.job_manager.submit("delivery", operation)
        except JobBusyError:
            if base_package is not None:
                base_package.unlink(missing_ok=True)
            return 409, {
                "ok": False,
                "error": {"code": "job_busy", "message": "已有任务正在运行，请稍后再试。"},
            }
        return 202, {"ok": True, "job_id": job_id, "kind": kind}

    def _download(self, download_id: str) -> tuple[int, object]:
        if self.download_registry is None:
            return 410, {
                "ok": False,
                "error": {"code": "download_gone", "message": "该下载已失效，请重新生成。"},
            }
        try:
            claim = self.download_registry.claim(download_id)
        except DownloadGoneError:
            return 410, {
                "ok": False,
                "error": {"code": "download_gone", "message": "该下载已失效，请重新生成。"},
            }
        return 200, FileResponse(
            path=claim.path,
            filename=claim.filename,
            content_type=claim.content_type,
            size=claim.size,
            sha256=claim.sha256,
            on_complete=lambda success: self.download_registry.complete(
                download_id, success
            ),
        )

    def _draft_preview(self, payload: object) -> tuple[int, dict]:
        if self.draft_upload_manager is None:
            return 503, {
                "ok": False,
                "error": {"code": "drafts_unavailable", "message": "稿件收件箱暂时不可用。"},
            }
        if not isinstance(payload, UploadedFile):
            return 400, {
                "ok": False,
                "error": {"code": "invalid_draft_upload", "message": "请选择一个 .inno-drafts 文件。"},
            }
        try:
            return 200, self.draft_upload_manager.preview(payload.filename, payload.path)
        except DraftUploadError as error:
            return error.status, {
                "ok": False,
                "error": {"code": error.code, "message": error.message},
            }

    def _accept_draft(self, receipt_id: str, payload: object) -> tuple[int, dict]:
        if self.draft_upload_manager is None:
            return 503, {
                "ok": False,
                "error": {"code": "drafts_unavailable", "message": "稿件收件箱暂时不可用。"},
            }
        confirm = payload.get("confirm") if isinstance(payload, dict) else None
        try:
            return 200, self.draft_upload_manager.accept(
                receipt_id,
                self.vault,
                confirm=confirm,
            )
        except DraftUploadError as error:
            return error.status, {
                "ok": False,
                "error": {"code": error.code, "message": error.message},
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
        if path == "/api/delivery" and method == "POST":
            return self._start_delivery(_payload)
        delivery_match = _DELIVERY_ROUTE.fullmatch(path)
        if delivery_match is not None and method in {"GET", "HEAD"}:
            return self._download(delivery_match.group(1))
        if path == "/api/drafts/preview" and method == "POST":
            return self._draft_preview(_payload)
        draft_match = _DRAFT_ACCEPT_ROUTE.fullmatch(path)
        if draft_match is not None and method == "POST":
            return self._accept_draft(draft_match.group(1), _payload)
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
