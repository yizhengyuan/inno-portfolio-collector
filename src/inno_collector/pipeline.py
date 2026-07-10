from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from .exporter import _sanitize
from .identity import article_key, select_since
from .ingest import ingest_account_output
from .models import (
    IngestResult,
    NormalizedArticle,
    PipelineRunResult,
    ProjectAccount,
    ProjectRunResult,
)
from .state import ManifestStore
from .vault import VaultWriter


class PipelineAuthenticationError(RuntimeError):
    pass


class PipelineConfigurationError(RuntimeError):
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


_ABSOLUTE_PATH = re.compile(
    r"(?<![:\w])/(?:Users|private|var|tmp)/[^\s|]+|"
    r"(?i:(?<![\w])(?:[A-Z]:\\)[^\s|]+)"
)


def _safe_error(error: BaseException) -> str:
    message = _sanitize(str(error)) or error.__class__.__name__
    return _ABSOLUTE_PATH.sub("[path]", message)


def _positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid numeric identifier")
    return value


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
    ) -> None:
        self.backend = backend
        self.runtime_dir = Path(runtime_dir)
        self.vault_root = self.runtime_dir / "vault" / "英诺被投项目资讯库"
        self.vault_writer = vault_writer
        self.ingest = ingest
        self.now = now
        self.sleep = sleep

    def _retry(self, operation: Callable[[], object]) -> object:
        for delay in (1.0, 3.0, None):
            try:
                return operation()
            except Exception:
                if delay is None:
                    raise
                self.sleep(delay)
        raise AssertionError("unreachable")

    def _existing_keys(self) -> set[str]:
        manifest_path = self.vault_root / "90-系统" / "manifest.json"
        if not manifest_path.exists():
            return set()
        try:
            store = ManifestStore(manifest_path)
        except (OSError, UnicodeError, ValueError):
            raise PipelineConfigurationError("existing manifest is invalid") from None
        return set(store.data["articles"])

    def _account_id(self, row: object) -> int:
        if not isinstance(row, dict):
            raise PipelineConfigurationError("resolved account is invalid")
        try:
            return _positive_id(row.get("id"))
        except ValueError:
            raise PipelineConfigurationError("resolved account is invalid") from None

    def _download_ids(
        self,
        rows: list[dict],
        existing_keys: set[str],
    ) -> tuple[list[int], set[str], int, int]:
        ids: list[int] = []
        expected_keys: set[str] = set()
        seen_ids: set[int] = set()
        skipped = 0
        failed = 0
        for row in rows:
            try:
                key = article_key(str(row.get("url") or ""))
            except ValueError:
                failed += 1
                continue
            if key in existing_keys:
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
            expected_keys.add(key)
        return ids, expected_keys, skipped, failed

    def _output_directory(self, payload: object, output_root: Path) -> Path:
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("exporter download was unsuccessful")
        if not isinstance(payload.get("output_dir"), str):
            raise RuntimeError("exporter returned invalid output directory")
        raw_output = payload["output_dir"].strip()
        if not raw_output:
            raise RuntimeError("exporter returned invalid output directory")
        try:
            root = output_root.resolve(strict=True)
            output = Path(raw_output).expanduser().resolve(strict=True)
            output.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise RuntimeError("exporter returned unsafe output directory") from None
        if not output.is_dir():
            raise RuntimeError("exporter returned invalid output directory")
        return output

    def _status(self, downloaded: int, skipped: int, failed: int) -> str:
        if failed == 0:
            return "success"
        if downloaded or skipped:
            return "partial"
        return "failed"

    def run(
        self,
        projects: Iterable[ProjectAccount],
        *,
        since: str,
        dry_run: bool = False,
    ) -> PipelineRunResult:
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
            accounts = self.backend.accounts()
        except Exception as exc:
            raise PipelineConfigurationError(_safe_error(exc)) from None
        if not isinstance(accounts, list):
            raise PipelineConfigurationError("exporter account list is invalid")

        existing_keys = self._existing_keys()
        all_articles: list[NormalizedArticle] = []
        all_results: list[ProjectRunResult] = []
        duplicate_count = 0
        accepted_keys: set[str] = set()

        for index, project in enumerate(project_list, start=1):
            attempted_at = self.now().isoformat()
            discovered = downloaded = skipped = failed = 0
            error = ""
            issues: list[str] = []
            try:
                account = self.backend.resolve_exact(project, accounts)
                account_id = self._account_id(account)
                if not dry_run:
                    self._retry(lambda: self.backend.sync(account_id, limit=1000))
                rows = self._retry(
                    lambda: self.backend.articles(account_id, limit=5000)
                )
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    raise RuntimeError("exporter returned invalid article list")
                selected = select_since(rows, since)
                discovered = len(selected)
                (
                    article_ids,
                    expected_keys,
                    skipped,
                    invalid_count,
                ) = self._download_ids(
                    selected, existing_keys
                )
                failed += invalid_count
                if invalid_count:
                    issues.append(
                        "article catalog contained "
                        f"{invalid_count} invalid or duplicate ids"
                    )

                if not dry_run and article_ids:
                    output_root = (
                        self.runtime_dir
                        / "staging"
                        / f"{index:02d}-{account_id}"
                    )
                    output_root.mkdir(parents=True, exist_ok=True)
                    payload = self._retry(
                        lambda: self.backend.download(article_ids, output_root)
                    )
                    output_dir = self._output_directory(payload, output_root)
                    ingested = self.ingest(project, output_dir)
                    outcome_keys: set[str] = set()
                    rejected_count = 0
                    for rejected in ingested.rejected:
                        try:
                            rejected_key = article_key(rejected.source_url)
                        except ValueError:
                            continue
                        if rejected_key in expected_keys:
                            outcome_keys.add(rejected_key)
                            failed += 1
                            rejected_count += 1
                    if rejected_count:
                        issues.append(
                            f"ingest rejected {rejected_count} requested article"
                            + ("s" if rejected_count != 1 else "")
                        )
                    for article in ingested.valid:
                        if article.key not in expected_keys:
                            continue
                        outcome_keys.add(article.key)
                        if article.key in accepted_keys or article.key in existing_keys:
                            duplicate_count += 1
                            skipped += 1
                            continue
                        accepted_keys.add(article.key)
                        all_articles.append(article)
                        downloaded += 1
                    missing_count = len(expected_keys - outcome_keys)
                    failed += missing_count
                    if missing_count:
                        issues.append(
                            f"download output omitted {missing_count} requested article"
                            + ("s" if missing_count != 1 else "")
                        )
                error = "; ".join(issues)
            except Exception as exc:
                failed = max(failed, 1)
                error = _safe_error(exc)

            all_results.append(
                ProjectRunResult(
                    project=project.project,
                    account=project.account,
                    discovered=discovered,
                    downloaded=downloaded,
                    skipped=skipped,
                    failed=failed,
                    status=self._status(downloaded, skipped, failed),
                    error=error,
                    last_sync=attempted_at,
                )
            )

        if not dry_run:
            writer = self.vault_writer or VaultWriter(self.vault_root)
            writer.apply(all_articles, all_results)

        failed_projects = sum(result.status != "success" for result in all_results)
        return PipelineRunResult(
            projects=tuple(all_results),
            project_count=len(all_results),
            failed_projects=failed_projects,
            article_count=len(all_articles),
            duplicate_count=duplicate_count,
        )
