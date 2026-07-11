from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .diagnostics import MAX_DIAGNOSTIC_LENGTH, sanitize_diagnostic
from .models import ProjectAccount


Runner = Callable[[list[str]], tuple[int, str, str]]
EXPORTER_TIMEOUT_SECONDS = 300
SPOOL_MAX_MEMORY_BYTES = 1 << 20
MAX_STDOUT_BYTES = 64 << 20
MAX_STDERR_BYTES = 1 << 20


class ExporterCommandError(RuntimeError):
    pass


class _ExporterOutputLimitError(RuntimeError):
    pass


def _read_output(stream: TextIO, max_bytes: int) -> str:
    stream.flush()
    stream.seek(0, 2)
    if stream.tell() > max_bytes:
        raise _ExporterOutputLimitError
    stream.seek(0)
    return stream.read()


def _default_runner(command: list[str]) -> tuple[int, str, str]:
    with tempfile.SpooledTemporaryFile(
        max_size=SPOOL_MAX_MEMORY_BYTES,
        mode="w+",
        encoding="utf-8",
    ) as stdout_file, tempfile.SpooledTemporaryFile(
        max_size=SPOOL_MAX_MEMORY_BYTES,
        mode="w+",
        encoding="utf-8",
    ) as stderr_file:
        result = subprocess.run(
            command,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
            check=False,
            timeout=EXPORTER_TIMEOUT_SECONDS,
        )
        stdout = _read_output(stdout_file, MAX_STDOUT_BYTES)
        stderr = _read_output(stderr_file, MAX_STDERR_BYTES)
    return result.returncode, stdout, stderr


def _sanitize(message: str) -> str:
    return sanitize_diagnostic(message)


def _object_list(payload: dict, field: str) -> list[dict]:
    rows = payload.get(field)
    if field not in payload or not isinstance(rows, list) or any(
        not isinstance(row, dict) for row in rows
    ):
        raise ExporterCommandError(f"exporter returned invalid {field}")
    return rows


class MooreExporterAdapter:
    def __init__(
        self,
        script: Path,
        runtime_dir: Path,
        runner: Runner = _default_runner,
        command_prefix: tuple[str, ...] | None = None,
    ) -> None:
        if command_prefix is not None and not command_prefix:
            raise ValueError("exporter command prefix must not be empty")
        self.script = script
        self.runtime_dir = runtime_dir
        self.runner = runner
        self.command_prefix = command_prefix or (sys.executable, str(self.script))

    def _execute(self, command: str, *arguments: str) -> tuple[int, dict, str]:
        argv = [
            *self.command_prefix,
            "--runtime-dir",
            str(self.runtime_dir),
            command,
            *arguments,
        ]
        try:
            code, stdout, stderr = self.runner(argv)
        except subprocess.TimeoutExpired:
            raise ExporterCommandError("exporter command timed out") from None
        except _ExporterOutputLimitError:
            raise ExporterCommandError("exporter output exceeded safe limit") from None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            message = stderr or stdout or str(exc)
            raise ExporterCommandError(_sanitize(message)) from exc

        if not isinstance(payload, dict):
            raise ExporterCommandError("exporter returned invalid JSON object")
        return code, payload, stderr

    def _run(self, command: str, *arguments: str) -> dict:
        code, payload, stderr = self._execute(command, *arguments)
        if code != 0 or payload.get("ok") is not True:
            message = payload.get("error") or stderr or "exporter command failed"
            raise ExporterCommandError(_sanitize(str(message)))
        return payload

    def _run_download(self, *arguments: str) -> dict:
        code, payload, stderr = self._execute("exporter-download", *arguments)
        if code == 0 and payload.get("ok") is True:
            return payload
        if code != 1:
            message = payload.get("error") or stderr or "exporter command failed"
            raise ExporterCommandError(_sanitize(str(message)))

        partial_fields = {
            "output_dir",
            "index",
            "selected_count",
            "success_count",
            "failure_count",
            "skipped_count",
            "skipped",
            "failed",
        }
        if not partial_fields.intersection(payload) and payload.get("error"):
            raise ExporterCommandError(_sanitize(str(payload["error"])))

        count_fields = (
            "selected_count",
            "success_count",
            "failure_count",
            "skipped_count",
        )
        counts = [payload.get(field) for field in count_fields]
        if (
            payload.get("ok") is not False
            or not isinstance(payload.get("output_dir"), str)
            or not payload["output_dir"].strip()
            or not isinstance(payload.get("index"), str)
            or not payload["index"].strip()
            or any(type(value) is not int or value < 0 for value in counts)
            or not isinstance(payload.get("skipped"), list)
            or any(not isinstance(item, dict) for item in payload["skipped"])
            or not isinstance(payload.get("failed"), list)
            or any(not isinstance(item, dict) for item in payload["failed"])
            or payload["failure_count"] <= 0
            or payload["failure_count"] != len(payload["failed"])
            or payload["skipped_count"] != len(payload["skipped"])
            or payload["selected_count"]
            != payload["success_count"]
            + payload["failure_count"]
            + payload["skipped_count"]
        ):
            raise ExporterCommandError(
                "exporter returned invalid partial download response"
            )
        return payload

    def _run_sync(self, account_id: int, *arguments: str) -> dict:
        code, payload, stderr = self._execute("exporter-sync", *arguments)
        if code == 0 and payload.get("ok") is True:
            return payload

        errors = payload.get("errors")
        fetched = payload.get("fetched_count")
        upserted = payload.get("upserted_count")
        if (
            code in {0, 1}
            and payload.get("ok") is False
            and type(payload.get("account_id")) is int
            and payload["account_id"] == account_id
            and type(fetched) is int
            and fetched >= 0
            and type(upserted) is int
            and 0 <= upserted <= fetched
            and isinstance(errors, list)
            and bool(errors)
            and all(isinstance(item, str) and item.strip() for item in errors)
        ):
            return {
                **payload,
                "errors": [_sanitize(item) for item in errors],
            }

        partial_fields = {
            "account_id",
            "fetched_count",
            "upserted_count",
            "errors",
        }
        if payload.get("ok") is False and partial_fields.intersection(payload):
            raise ExporterCommandError(
                "exporter returned invalid partial sync response"
            )
        message = payload.get("error") or stderr or "exporter command failed"
        raise ExporterCommandError(_sanitize(str(message)))

    def auth_check(self) -> dict:
        return self._run("exporter-auth-check")

    def accounts(self) -> list[dict]:
        return _object_list(self._run("exporter-accounts"), "accounts")

    def sync(self, account_id: int, limit: int = 1000) -> dict:
        return self._run_sync(
            account_id,
            "--account-id",
            str(account_id),
            "--limit",
            str(limit),
        )

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
        payload = self._run(
            "exporter-articles",
            "--account-id",
            str(account_id),
            "--limit",
            str(limit),
        )
        return _object_list(payload, "articles")

    def download(self, article_ids: list[int], output_root: Path) -> dict:
        joined_ids = ",".join(str(article_id) for article_id in article_ids)
        return self._run_download(
            "--article-ids",
            joined_ids,
            "--output-dir",
            str(output_root),
        )

    def resolve_exact(self, project: ProjectAccount, rows: list[dict]) -> dict:
        expected_nicknames = {
            value.strip().casefold()
            for value in (project.account, *project.aliases)
            if value.strip()
        }
        expected_alias = project.wechat_id.strip().casefold()
        matches = []
        for row in rows:
            nickname = str(row.get("nickname", "") or "").strip().casefold()
            alias = str(row.get("alias", "") or "").strip().casefold()
            if nickname in expected_nicknames or (
                expected_alias and alias == expected_alias
            ):
                matches.append(row)

        if len(matches) != 1:
            raise ExporterCommandError(
                f"expected one exact account match for {project.project}, got {len(matches)}"
            )
        return matches[0]
