from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .models import ProjectAccount


Runner = Callable[[list[str]], tuple[int, str, str]]
SECRET_RE = re.compile(
    r"(?i)(auth-key|pass_ticket|appmsg_token|token|ticket|uin)=([^&\s\"']+)"
)


class ExporterCommandError(RuntimeError):
    pass


def _default_runner(command: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _sanitize(message: str) -> str:
    return SECRET_RE.sub(r"\1=[REDACTED]", message)


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
        code, stdout, stderr = self.runner(argv)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            message = stderr or stdout or str(exc)
            raise ExporterCommandError(_sanitize(message)) from exc

        if code != 0 or not payload.get("ok", False):
            message = payload.get("error") or stderr or f"{command} failed"
            raise ExporterCommandError(_sanitize(str(message)))
        return payload

    def auth_check(self) -> dict:
        return self._run("exporter-auth-check")

    def accounts(self) -> list[dict]:
        return list(self._run("exporter-accounts").get("accounts", []))

    def sync(self, account_id: int, limit: int = 1000) -> dict:
        return self._run(
            "exporter-sync",
            "--account-id",
            str(account_id),
            "--limit",
            str(limit),
        )

    def articles(self, account_id: int, limit: int = 5000) -> list[dict]:
        return list(
            self._run(
                "exporter-articles",
                "--account-id",
                str(account_id),
                "--limit",
                str(limit),
            ).get("articles", [])
        )

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
        expected = {
            value.strip().casefold()
            for value in (project.account, project.wechat_id, *project.aliases)
            if value.strip()
        }
        matches = []
        for row in rows:
            actual = {
                str(row.get(field, "") or "").strip().casefold()
                for field in ("nickname", "alias")
            }
            if expected & actual:
                matches.append(row)

        if len(matches) != 1:
            raise ExporterCommandError(
                f"expected one exact account match for {project.project}, got {len(matches)}"
            )
        return matches[0]
