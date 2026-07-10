from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .models import ProjectAccount


Runner = Callable[[list[str]], tuple[int, str, str]]
MAX_DIAGNOSTIC_LENGTH = 4096
EXPORTER_TIMEOUT_SECONDS = 300
SECRET_RE = re.compile(
    r"(?i)(auth-key|pass_ticket|appmsg_token|token|ticket|uin)=([^&\s\"']+)"
)
DELIMITED_SECRET_RE = re.compile(
    r"(?i)(?<![\w-])((?:\"|')?(?:auth-key|pass_ticket|appmsg_token|token|ticket|uin)"
    r"(?:\"|')?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^&\s,\"']+)"
)
AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,\"']+)"
)
CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:auth-key|pass_ticket|appmsg_token|token|ticket|uin)\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"
)


class ExporterCommandError(RuntimeError):
    pass


def _default_runner(command: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=EXPORTER_TIMEOUT_SECONDS,
    )
    return result.returncode, result.stdout, result.stderr


def _sanitize(message: str) -> str:
    sanitized = DELIMITED_SECRET_RE.sub(r"\1[REDACTED]", message)
    sanitized = AUTHORIZATION_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = CLI_SECRET_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = SECRET_RE.sub(r"\1=[REDACTED]", sanitized)
    return sanitized[:MAX_DIAGNOSTIC_LENGTH]


def _object_list(payload: dict, field: str) -> list[dict]:
    rows = payload.get(field, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ExporterCommandError(f"exporter returned invalid {field}")
    return rows


class MooreExporterAdapter:
    def __init__(
        self,
        script: Path,
        runtime_dir: Path,
        runner: Runner = _default_runner,
    ) -> None:
        self.script = script
        self.runtime_dir = runtime_dir
        self.runner = runner

    def _run(self, command: str, *arguments: str) -> dict:
        argv = [
            sys.executable,
            str(self.script),
            "--runtime-dir",
            str(self.runtime_dir),
            command,
            *arguments,
        ]
        try:
            code, stdout, stderr = self.runner(argv)
        except subprocess.TimeoutExpired:
            raise ExporterCommandError("exporter command timed out") from None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            message = stderr or stdout or str(exc)
            raise ExporterCommandError(_sanitize(message)) from exc

        if not isinstance(payload, dict):
            raise ExporterCommandError("exporter returned invalid JSON object")
        if code != 0 or payload.get("ok") is not True:
            message = payload.get("error") or stderr or "exporter command failed"
            raise ExporterCommandError(_sanitize(str(message)))
        return payload

    def auth_check(self) -> dict:
        return self._run("exporter-auth-check")

    def accounts(self) -> list[dict]:
        return _object_list(self._run("exporter-accounts"), "accounts")

    def sync(self, account_id: int, limit: int = 1000) -> dict:
        return self._run(
            "exporter-sync",
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
        return self._run(
            "exporter-download",
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
