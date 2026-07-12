from __future__ import annotations

import errno
import hashlib
import json
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .diagnostics import sanitize_diagnostic
from .identity import (
    article_key,
    canonical_url,
    select_since_with_invalid_urls,
)
from .ingest import ingest_account_output
from .models import (
    IngestResult,
    NormalizedArticle,
    PipelineRunResult,
    ProjectAccount,
    ProjectRunResult,
)
from .state import CatalogStateStore, ManifestStore
from .vault import VaultWriter


class PipelineAuthenticationError(RuntimeError):
    pass


class PipelineConfigurationError(RuntimeError):
    pass


class PipelineDeliveryError(RuntimeError):
    pass


class PipelineCancelledError(RuntimeError):
    pass


class _Backend(Protocol):
    def auth_check(self) -> object: ...

    def accounts(self) -> list[dict]: ...

    def resolve_exact(self, project: ProjectAccount, rows: list[dict]) -> dict: ...

    def sync(self, account_id: int, limit: int = 1000) -> dict: ...

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]: ...

    def download(self, article_ids: list[int], output_root: Path) -> dict: ...


class _VaultWriter(Protocol):
    def apply(
        self,
        articles: list[NormalizedArticle],
        project_results: list[ProjectRunResult],
    ) -> object: ...


_TRANSIENT_STATUS = re.compile(r"(?<!\d)(?:429|5\d\d)(?!\d)")
_TRANSIENT_TEXT = (
    "timed out",
    "timeout",
    "temporary",
    "temporarily",
    "unavailable",
    "connection reset",
    "connection refused",
    "try again",
    "rate limit",
    "too many requests",
)
_AUTHORIZATION_TEXT = (
    "authentication",
    "authorization",
    "forbidden",
    "permission",
)
_CONFIGURATION_TEXT = (
    "format",
    "unsafe",
    "account match",
)
_NON_TRANSIENT_TEXT = (
    "invalid",
)
_TRANSIENT_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
}
_VOLATILE_CATALOG_FIELDS = {
    "content_downloaded",
    "created_at",
    "downloaded_at",
    "fetch_time",
    "fetched_at",
    "sync_time",
    "synced_at",
    "updated_at",
}
_STABLE_CATALOG_FIELDS = (
    "title",
    "publish_time",
    "digest",
    "author",
    "msgid",
    "idx",
    "cover_url",
    "is_deleted",
    "article_status",
    "is_original",
    "collection_title",
)


def _safe_error(error: BaseException) -> str:
    return sanitize_diagnostic(error, fallback=error.__class__.__name__)


def _positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid numeric identifier")
    return value


def _stable_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _stable_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).casefold() not in _VOLATILE_CATALOG_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    return str(value)


def _stable_raw_json(value: object) -> object:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    return _stable_json_value(value)


def catalog_fingerprint(row: dict) -> str:
    stable = {
        "url": canonical_url(str(row.get("url") or "")),
        **{
            field: str(row.get(field) or "").strip()
            for field in _STABLE_CATALOG_FIELDS
        },
        "raw_json": _stable_raw_json(row.get("raw_json")),
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _is_transient(error: Exception) -> bool:
    if isinstance(error, (PipelineAuthenticationError, PipelineConfigurationError)):
        return False
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OSError) and error.errno in _TRANSIENT_ERRNOS:
        return True
    message = str(error).casefold()
    if (
        re.search(r"(?<!\d)(?:401|403)(?!\d)", message) is not None
        or any(marker in message for marker in _AUTHORIZATION_TEXT)
        or any(marker in message for marker in _CONFIGURATION_TEXT)
    ):
        return False
    if _TRANSIENT_STATUS.search(message) is not None:
        return True
    if any(marker in message for marker in _NON_TRANSIENT_TEXT):
        return False
    return any(
        marker in message for marker in _TRANSIENT_TEXT
    )


def _status(downloaded: int, skipped: int, failed: int) -> str:
    if failed == 0:
        return "success"
    if downloaded or skipped:
        return "partial"
    return "failed"


def _cached_catalog_covers_cutoff(rows: list[dict], cutoff: date) -> bool:
    for row in rows:
        try:
            article_key(str(row.get("url") or ""))
        except (AttributeError, TypeError, ValueError):
            continue
        published_text = str(row.get("publish_time") or "").strip()
        if not published_text:
            continue
        try:
            published = datetime.fromisoformat(
                published_text.replace("Z", "+00:00")
            ).date()
        except ValueError:
            try:
                published = date.fromisoformat(published_text[:10])
            except ValueError:
                continue
        if published < cutoff:
            return True
    return False


class CollectionPipeline:
    def __init__(
        self,
        backend: _Backend,
        runtime_dir: Path = Path("runtime"),
        *,
        vault_writer: _VaultWriter | None = None,
        ingest: Callable[[ProjectAccount, Path], IngestResult] = ingest_account_output,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        sleep: Callable[[float], None] = time.sleep,
        verification_interval: timedelta = timedelta(hours=24),
    ) -> None:
        if (
            not isinstance(verification_interval, timedelta)
            or verification_interval < timedelta(0)
        ):
            raise ValueError("verification interval must be non-negative")
        self.backend = backend
        self.runtime_dir = Path(runtime_dir)
        self.vault_root = self.runtime_dir / "vault" / "英诺被投项目资讯库"
        self.vault_writer = vault_writer
        self.ingest = ingest
        self.now = now
        self.sleep = sleep
        self.verification_interval = verification_interval

    def _retry(self, operation: Callable[[], object]) -> object:
        for delay in (1.0, 3.0, None):
            try:
                return operation()
            except Exception as exc:
                if delay is None or not _is_transient(exc):
                    raise
                self.sleep(delay)
        raise AssertionError("unreachable")

    def _manifest_records(self) -> dict[str, dict]:
        manifest_path = self.vault_root / "90-系统" / "manifest.json"
        if not manifest_path.exists():
            return {}
        try:
            return ManifestStore(manifest_path).data["articles"]
        except (OSError, UnicodeError, ValueError):
            raise PipelineConfigurationError("existing manifest is invalid") from None

    def _account_id(self, row: object) -> int:
        if not isinstance(row, dict):
            raise PipelineConfigurationError("resolved account is invalid")
        try:
            return _positive_id(row.get("id"))
        except ValueError:
            raise PipelineConfigurationError("resolved account is invalid") from None

    def _download_plan(
        self,
        rows: list[dict],
        manifest_records: dict[str, dict],
        catalog_state: CatalogStateStore,
        verification_time: datetime,
    ) -> tuple[list[int], dict[int, str], dict[str, str], int, int]:
        ids: list[int] = []
        requested_keys: dict[int, str] = {}
        fingerprints: dict[str, str] = {}
        seen_ids: set[int] = set()
        skipped = 0
        failed = 0
        for row in rows:
            try:
                key = article_key(str(row.get("url") or ""))
                fingerprint = catalog_fingerprint(row)
            except (TypeError, ValueError):
                failed += 1
                continue
            state_record = catalog_state.get_record(key)
            manifest_record = manifest_records.get(key)
            verified_at: datetime | None = None
            if state_record is not None and isinstance(
                state_record.get("verified_at"), str
            ):
                try:
                    verified_at = datetime.fromisoformat(
                        state_record["verified_at"].replace("Z", "+00:00")
                    )
                except ValueError:
                    verified_at = None
            age = (
                None
                if verified_at is None
                else verification_time - verified_at.astimezone(
                    verification_time.tzinfo
                )
            )
            if (
                manifest_record is not None
                and state_record is not None
                and state_record.get("fingerprint") == fingerprint
                and isinstance(state_record.get("content_hash"), str)
                and manifest_record.get("content_hash")
                == state_record["content_hash"]
                and age is not None
                and timedelta(0) <= age < self.verification_interval
            ):
                skipped += 1
                continue
            try:
                article_id = _positive_id(row.get("id"))
            except ValueError:
                failed += 1
                continue
            if article_id in seen_ids:
                failed += 1
                continue
            seen_ids.add(article_id)
            ids.append(article_id)
            requested_keys[article_id] = key
            fingerprints[key] = fingerprint
        return ids, requested_keys, fingerprints, skipped, failed

    def _secure_directory(
        self,
        path: Path,
        *,
        parent: Path | None = None,
    ) -> Path:
        try:
            details = path.lstat()
        except FileNotFoundError:
            try:
                if parent is None:
                    path.mkdir(parents=True)
                else:
                    path.mkdir()
                details = path.lstat()
            except OSError:
                raise PipelineConfigurationError(
                    "unsafe runtime staging directory"
                ) from None
        except OSError:
            raise PipelineConfigurationError(
                "unsafe runtime staging directory"
            ) from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise PipelineConfigurationError("unsafe runtime staging directory")
        try:
            resolved = path.resolve(strict=True)
            if parent is not None and resolved.parent != parent:
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise PipelineConfigurationError(
                "unsafe runtime staging directory"
            ) from None
        return resolved

    def _runtime_layout(
        self,
        resolved_accounts: list[int | None],
    ) -> tuple[Path, Path, dict[int, Path], dict[int, str]]:
        runtime_root = self._secure_directory(self.runtime_dir)
        staging_root = self._secure_directory(
            runtime_root / "staging", parent=runtime_root
        )
        state_root = self._secure_directory(runtime_root / "state", parent=runtime_root)
        account_roots: dict[int, Path] = {}
        errors: dict[int, str] = {}
        for index, account_id in enumerate(resolved_accounts, start=1):
            if account_id is None:
                continue
            try:
                account_roots[index - 1] = self._secure_directory(
                    staging_root / f"{index:02d}-{account_id}",
                    parent=staging_root,
                )
            except PipelineConfigurationError as exc:
                errors[index - 1] = str(exc)
        return runtime_root, state_root, account_roots, errors

    def _temporary_output_root(self, account_root: Path) -> Path:
        try:
            run_root = Path(tempfile.mkdtemp(dir=account_root, prefix="run-"))
            details = run_root.lstat()
            resolved = run_root.resolve(strict=True)
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISDIR(details.st_mode)
                or resolved.parent != account_root
            ):
                raise ValueError
            return resolved
        except (OSError, RuntimeError, ValueError):
            raise PipelineConfigurationError(
                "unsafe runtime staging directory"
            ) from None

    def _cleanup_output_root(self, run_root: Path) -> None:
        try:
            details = run_root.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(details.st_mode):
            run_root.unlink()
        elif stat.S_ISDIR(details.st_mode):
            shutil.rmtree(run_root)
        else:
            run_root.unlink()

    def _download_output(
        self,
        payload: object,
        output_root: Path,
        requested_keys: dict[int, str],
    ) -> tuple[Path, set[str], set[str], set[str]]:
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise PipelineConfigurationError("exporter returned invalid download response")
        count_fields = (
            "selected_count",
            "success_count",
            "failure_count",
            "skipped_count",
        )
        counts = [payload.get(field) for field in count_fields]
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or payload["selected_count"] != len(requested_keys)
            or payload["selected_count"]
            != payload["success_count"]
            + payload["failure_count"]
            + payload["skipped_count"]
            or not isinstance(payload.get("failed"), list)
            or any(not isinstance(item, dict) for item in payload["failed"])
            or len(payload["failed"]) != payload["failure_count"]
            or not isinstance(payload.get("skipped"), list)
            or any(not isinstance(item, dict) for item in payload["skipped"])
            or len(payload["skipped"]) != payload["skipped_count"]
            or (payload["ok"] and payload["failure_count"] != 0)
            or (not payload["ok"] and payload["failure_count"] == 0)
            or not isinstance(payload.get("output_dir"), str)
            or not payload["output_dir"].strip()
            or not isinstance(payload.get("index"), str)
            or not payload["index"].strip()
        ):
            raise PipelineConfigurationError("exporter returned invalid download response")
        try:
            root_details = output_root.lstat()
            root = output_root.resolve(strict=True)
            if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(
                root_details.st_mode
            ):
                raise ValueError
            output_path = Path(payload["output_dir"]).expanduser()
            output_details = output_path.lstat()
            if stat.S_ISLNK(output_details.st_mode) or not stat.S_ISDIR(
                output_details.st_mode
            ):
                raise ValueError
            output = output_path.resolve(strict=True)
            output.relative_to(root)
            index_path = Path(payload["index"]).expanduser()
            try:
                index_details = index_path.lstat()
            except FileNotFoundError:
                index_details = None
            if index_details is not None and (
                stat.S_ISLNK(index_details.st_mode)
                or not stat.S_ISREG(index_details.st_mode)
            ):
                raise ValueError
            index = index_path.resolve(strict=False)
            index.relative_to(output)
            if index != (output / "index.csv").resolve(strict=False):
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise PipelineConfigurationError(
                "exporter returned unsafe output directory"
            ) from None
        if not output.is_dir():
            raise PipelineConfigurationError(
                "exporter returned invalid output directory"
            )
        requested = set(requested_keys.values())

        def item_key(item: dict) -> str:
            candidates: set[str] = set()
            for field in ("article_id", "db_article_id"):
                if field not in item:
                    continue
                try:
                    article_id = _positive_id(item[field])
                    candidates.add(requested_keys[article_id])
                except (KeyError, ValueError):
                    raise PipelineConfigurationError(
                        "exporter returned invalid download response"
                    ) from None
            for field in ("source_url", "url"):
                if field not in item:
                    continue
                try:
                    key = article_key(str(item[field] or ""))
                except ValueError:
                    raise PipelineConfigurationError(
                        "exporter returned invalid download response"
                    ) from None
                if key not in requested:
                    raise PipelineConfigurationError(
                        "exporter returned invalid download response"
                    )
                candidates.add(key)
            if len(candidates) != 1:
                raise PipelineConfigurationError(
                    "exporter returned invalid download response"
                )
            return next(iter(candidates))

        failed_keys = {item_key(item) for item in payload["failed"]}
        skipped_keys = {item_key(item) for item in payload["skipped"]}
        if (
            len(failed_keys) != len(payload["failed"])
            or len(skipped_keys) != len(payload["skipped"])
            or failed_keys.intersection(skipped_keys)
        ):
            raise PipelineConfigurationError(
                "exporter returned invalid download response"
            )
        success_keys = requested - failed_keys - skipped_keys
        if len(success_keys) != payload["success_count"]:
            raise PipelineConfigurationError(
                "exporter returned invalid download response"
            )
        return output, failed_keys, skipped_keys, success_keys

    def _project_result(
        self,
        project: ProjectAccount,
        discovered: int,
        downloaded: int,
        skipped: int,
        failed: int,
        error: str,
        last_sync: str,
        *,
        force_partial: bool = False,
    ) -> ProjectRunResult:
        return ProjectRunResult(
            project=project.project,
            account=project.account,
            discovered=discovered,
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            status=(
                "partial"
                if force_partial and failed > 0
                else _status(downloaded, skipped, failed)
            ),
            error=error,
            last_sync=last_sync,
        )

    def run(
        self,
        projects: Iterable[ProjectAccount],
        *,
        since: str,
        dry_run: bool = False,
        progress: Callable[[dict[str, object]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PipelineRunResult:
        def emit(
            event_type: str,
            *,
            project: str = "",
            stage: str = "",
            counts: dict[str, int] | None = None,
        ) -> None:
            if progress is not None:
                progress(
                    {
                        "type": event_type,
                        "project": project,
                        "stage": stage,
                        "counts": counts or {},
                    }
                )

        def checkpoint() -> None:
            if cancelled is not None and cancelled():
                raise PipelineCancelledError("collection was cancelled")

        project_list = list(projects)
        try:
            parsed_since = date.fromisoformat(since)
        except (TypeError, ValueError):
            raise PipelineConfigurationError("since must be an ISO date") from None
        if parsed_since.isoformat() != since:
            raise PipelineConfigurationError("since must be an ISO date")

        try:
            auth = self.backend.auth_check()
        except Exception as exc:
            raise PipelineAuthenticationError(_safe_error(exc)) from None
        if (
            not isinstance(auth, dict)
            or auth.get("ok") is not True
            or auth.get("status") != "valid"
        ):
            raise PipelineAuthenticationError(
                "exporter authentication is not valid"
            )
        try:
            accounts = self._retry(self.backend.accounts)
        except Exception as exc:
            raise PipelineConfigurationError(
                "accounts: " + _safe_error(exc)
            ) from None
        if not isinstance(accounts, list):
            raise PipelineConfigurationError("exporter account list is invalid")

        resolved_ids: list[int | None] = []
        resolution_errors: dict[int, str] = {}
        for index, project in enumerate(project_list):
            try:
                resolved_ids.append(
                    self._account_id(self.backend.resolve_exact(project, accounts))
                )
            except Exception as exc:
                resolved_ids.append(None)
                resolution_errors[index] = "resolve: " + _safe_error(exc)

        state_path = self.runtime_dir / "state" / "catalog-state.json"
        account_roots: dict[int, Path] = {}
        if not dry_run:
            try:
                _, state_root, account_roots, staging_errors = self._runtime_layout(
                    resolved_ids
                )
            except PipelineConfigurationError as exc:
                raise PipelineConfigurationError(
                    "staging: " + _safe_error(exc)
                ) from None
            staging_errors = {
                index: "staging: " + sanitize_diagnostic(message)
                for index, message in staging_errors.items()
            }
            resolution_errors.update(staging_errors)
            state_path = state_root / "catalog-state.json"
        elif self.runtime_dir.exists():
            runtime_details = self.runtime_dir.lstat()
            if stat.S_ISLNK(runtime_details.st_mode) or not stat.S_ISDIR(
                runtime_details.st_mode
            ):
                raise PipelineConfigurationError(
                    "staging: unsafe runtime staging directory"
                )

        try:
            catalog_state = CatalogStateStore(state_path)
        except (OSError, UnicodeError, ValueError):
            raise PipelineConfigurationError(
                "catalog: existing catalog state is invalid"
            ) from None
        try:
            manifest_records = self._manifest_records()
        except PipelineConfigurationError as exc:
            raise PipelineConfigurationError(
                "catalog: " + _safe_error(exc)
            ) from None
        accepted_keys: set[str] = set()
        seen_catalog_keys: set[str] = set()
        article_count = 0
        duplicate_count = 0
        results: list[ProjectRunResult] = []
        writer = self.vault_writer or VaultWriter(self.vault_root)
        verification_time = self.now()
        if verification_time.tzinfo is None or verification_time.utcoffset() is None:
            verification_time = verification_time.astimezone()

        for index, project in enumerate(project_list):
            checkpoint()
            emit("project_started", project=project.project, stage="mapping")
            if index in resolution_errors:
                result = self._project_result(
                    project,
                    0,
                    0,
                    0,
                    1,
                    resolution_errors[index],
                    "",
                )
                results.append(result)
                emit(
                    "project_finished",
                    project=project.project,
                    stage="mapping",
                    counts={"failed": result.failed},
                )
                continue

            account_id = resolved_ids[index]
            assert account_id is not None
            discovered = downloaded = skipped = failed = 0
            last_sync = ""
            error = ""
            issues: list[str] = []
            article_ids: list[int] = []
            pending_failures = 0
            run_root: Path | None = None
            vault_succeeded = False
            cleanup_error: Exception | None = None
            partial_sync_covered = False
            stage = "catalog" if dry_run else "sync"
            try:
                partial_sync = False
                partial_fetched = 0
                if not dry_run:
                    sync_payload = self._retry(
                        lambda: self.backend.sync(account_id, limit=1000)
                    )
                    if not isinstance(sync_payload, dict):
                        raise PipelineConfigurationError(
                            "exporter returned unsuccessful sync response"
                        )
                    if sync_payload.get("ok") is True:
                        last_sync = self.now().isoformat()
                    elif sync_payload.get("ok") is False:
                        fetched = sync_payload.get("fetched_count")
                        errors = sync_payload.get("errors")
                        if (
                            type(fetched) is not int
                            or fetched <= 0
                            or not isinstance(errors, list)
                            or not errors
                            or any(
                                not isinstance(item, str) or not item.strip()
                                for item in errors
                            )
                        ):
                            raise PipelineConfigurationError(
                                "exporter returned unsuccessful sync response"
                            )
                        partial_sync = True
                        partial_fetched = fetched
                        failed += 1
                        issues.append(
                            "sync: partial "
                            + sanitize_diagnostic("; ".join(errors))
                        )
                    else:
                        raise PipelineConfigurationError(
                            "exporter returned unsuccessful sync response"
                        )

                stage = "catalog"
                rows = self._retry(
                    lambda: self.backend.articles(account_id, limit=5000)
                )
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    raise PipelineConfigurationError(
                        "exporter returned invalid article list"
                    )
                if partial_sync and (
                    partial_fetched <= 0
                    or not _cached_catalog_covers_cutoff(rows, parsed_since)
                ):
                    raise PipelineConfigurationError(
                        "partial sync cache does not cover cutoff"
                    )
                partial_sync_covered = partial_sync
                selected, invalid_url_count = select_since_with_invalid_urls(
                    rows, since
                )
                emit(
                    "catalog_synced",
                    project=project.project,
                    stage="catalog",
                    counts={"discovered": len(rows)},
                )
                for row in selected:
                    key = article_key(str(row.get("url") or ""))
                    if key in seen_catalog_keys:
                        duplicate_count += 1
                    else:
                        seen_catalog_keys.add(key)
                discovered = len(selected) + invalid_url_count
                failed += invalid_url_count
                if invalid_url_count:
                    issues.append(
                        f"catalog: {invalid_url_count} invalid or missing urls"
                    )
                (
                    article_ids,
                    requested_keys,
                    fingerprints,
                    skipped,
                    invalid_count,
                ) = self._download_plan(
                    selected,
                    manifest_records,
                    catalog_state,
                    verification_time,
                )
                failed += invalid_count
                if invalid_count:
                    issues.append(
                        f"catalog: {invalid_count} invalid or duplicate ids"
                    )
                emit(
                    "articles_selected",
                    project=project.project,
                    stage="catalog",
                    counts={
                        "selected": len(article_ids),
                        "skipped": skipped,
                        "failed": failed,
                    },
                )

                if not dry_run and article_ids:
                    checkpoint()
                    pending_failures = len(article_ids)
                    stage = "staging"
                    run_root = self._temporary_output_root(account_roots[index])
                    stage = "download"
                    payload = self._retry(
                        lambda: self.backend.download(article_ids, run_root)
                    )
                    checkpoint()
                    (
                        output_dir,
                        payload_failed_keys,
                        payload_skipped_keys,
                        success_keys,
                    ) = self._download_output(
                        payload, run_root, requested_keys
                    )
                    skipped += len(payload_skipped_keys)
                    emit(
                        "download_progress",
                        project=project.project,
                        stage="download",
                        counts={
                            "selected": len(article_ids),
                            "downloaded": len(success_keys),
                            "skipped": len(payload_skipped_keys),
                            "failed": len(payload_failed_keys),
                        },
                    )
                    stage = "ingest"
                    ingested = self.ingest(project, output_dir)
                    expected_keys = success_keys
                    outcomes: dict[str, tuple[str, object]] = {}
                    conflicting_keys: set[str] = set()

                    def record_outcome(
                        key: str, kind: str, value: object
                    ) -> None:
                        if key not in expected_keys:
                            return
                        if key in outcomes:
                            conflicting_keys.add(key)
                            return
                        outcomes[key] = (kind, value)

                    for rejected in ingested.rejected:
                        try:
                            rejected_key = article_key(rejected.source_url)
                        except ValueError:
                            continue
                        record_outcome(rejected_key, "rejected", rejected)
                    for article in ingested.valid:
                        record_outcome(article.key, "valid", article)

                    rejected_keys = {
                        key
                        for key, (kind, _) in outcomes.items()
                        if kind == "rejected" and key not in conflicting_keys
                    }
                    missing_keys = expected_keys - set(outcomes)
                    candidates: list[NormalizedArticle] = []
                    for key, (kind, value) in outcomes.items():
                        if kind != "valid" or key in conflicting_keys:
                            continue
                        assert isinstance(value, NormalizedArticle)
                        if key in accepted_keys:
                            skipped += 1
                            continue
                        candidates.append(value)
                    pending_failures = len(candidates)
                    conflict_count = len(conflicting_keys)
                    rejected_count = len(rejected_keys)
                    missing_count = len(missing_keys)
                    payload_failures = len(payload_failed_keys)
                    batch_failures = (
                        payload_failures
                        + conflict_count
                        + rejected_count
                        + missing_count
                    )
                    failed += batch_failures
                    if conflict_count:
                        issues.append(
                            "ingest: conflicting or duplicate outcome for "
                            f"{conflict_count} requested article"
                            + ("s" if conflict_count != 1 else "")
                        )
                    if rejected_count:
                        issues.append(
                            f"ingest: rejected {rejected_count} requested article"
                            + ("s" if rejected_count != 1 else "")
                        )
                    if missing_count:
                        issues.append(
                            f"ingest: output omitted {missing_count} requested article"
                            + ("s" if missing_count != 1 else "")
                        )
                    if payload_failures:
                        issues.append(
                            f"download: exporter failed {payload_failures} requested article"
                            + ("s" if payload_failures != 1 else "")
                        )

                    if candidates:
                        provisional = self._project_result(
                            project,
                            discovered,
                            len(candidates),
                            skipped,
                            failed,
                            "; ".join(issues),
                            last_sync,
                            force_partial=partial_sync_covered,
                        )
                        stage = "vault"
                        writer.apply(candidates, [provisional])
                        vault_succeeded = True
                        pending_failures = 0
                        downloaded = len(candidates)
                        accepted_keys.update(article.key for article in candidates)
                        article_count += downloaded
                        verified_at = self.now()
                        if (
                            verified_at.tzinfo is None
                            or verified_at.utcoffset() is None
                        ):
                            verified_at = verified_at.astimezone()
                        stage = "state"
                        for article in candidates:
                            catalog_state.mark_success(
                                article.key,
                                fingerprints[article.key],
                                content_hash=article.content_hash,
                                verified_at=verified_at.isoformat(),
                            )
                        catalog_state.save()
                        for article in candidates:
                            manifest_records[article.key] = {
                                "content_hash": article.content_hash
                            }
                error = "; ".join(issues)
            except PipelineCancelledError:
                raise
            except Exception as exc:
                if not vault_succeeded:
                    downloaded = 0
                    failed += pending_failures or 1
                else:
                    failed = max(failed, 1)
                exception_message = _safe_error(exc)
                issue_message = "; ".join(issues)
                staged_exception = f"{stage}: {exception_message}"
                error = (
                    f"{issue_message}; {staged_exception}"
                    if issue_message
                    else staged_exception
                )
            finally:
                if run_root is not None:
                    try:
                        self._cleanup_output_root(run_root)
                    except Exception as exc:
                        cleanup_error = exc

            if cleanup_error is not None:
                failed = max(failed, 1)
                cleanup_message = (
                    "cleanup: " + _safe_error(cleanup_error)
                )
                error = (
                    f"{error}; {cleanup_message}" if error else cleanup_message
                )

            result = self._project_result(
                project,
                discovered,
                downloaded,
                skipped,
                failed,
                error,
                last_sync,
                force_partial=partial_sync_covered,
            )
            results.append(result)
            emit(
                "project_finished",
                project=project.project,
                stage="complete",
                counts={
                    "discovered": result.discovered,
                    "downloaded": result.downloaded,
                    "skipped": result.skipped,
                    "failed": result.failed,
                },
            )

        if not dry_run:
            try:
                writer.apply([], results)
            except Exception:
                raise PipelineDeliveryError(
                    "report: failed to rebuild collection report"
                ) from None
            emit(
                "validation_finished",
                stage="validation",
                counts={
                    "project_count": len(results),
                    "article_count": article_count,
                    "failed_projects": sum(
                        result.status != "success" for result in results
                    ),
                },
            )

        failed_projects = sum(result.status != "success" for result in results)
        return PipelineRunResult(
            projects=tuple(results),
            project_count=len(results),
            failed_projects=failed_projects,
            article_count=article_count,
            duplicate_count=duplicate_count,
        )
