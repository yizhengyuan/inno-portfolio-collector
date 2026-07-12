from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .. import __version__
from ..diagnostics import sanitize_diagnostic
from ..package import lint_vault
from .responses import WebResponse


Linter = Callable[[Path], dict[str, object]]
BooleanProvider = Callable[[], bool]
JobProvider = Callable[[], dict[str, object] | None]
_ASSET_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8", True),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8", False),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8", False),
}
_MAX_ASSET_BYTES = 1 << 20


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
    ) -> None:
        self.vault = Path(vault)
        self._authenticated = authenticated or (lambda: False)
        self._recent_job = recent_job or (lambda: None)
        self._linter = linter
        self.assets_root = assets_root or Path(__file__).with_name("assets")

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
            "capabilities": ["read_library"],
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
        if method not in {"GET", "HEAD"}:
            return 405, {
                "ok": False,
                "error": {"code": "method_not_allowed", "message": "Method not allowed."},
            }
        if path == "/api/bootstrap":
            return self._bootstrap()
        if path == "/api/library/summary":
            return self._library_summary()
        if path in _ASSET_ROUTES:
            return self._asset(path)
        return _not_found()
