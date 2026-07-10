from __future__ import annotations

import json
from pathlib import Path

from .models import ProjectAccount


def _string(value: object) -> str:
    return "" if value is None else str(value).strip()


def load_projects(path: Path) -> tuple[ProjectAccount, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("projects config must be a JSON array")

    projects: list[ProjectAccount] = []
    project_names: set[str] = set()
    account_names: set[str] = set()

    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("project config item must be a JSON object")

        enabled_raw = item.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise ValueError("enabled must be a JSON boolean")
        enabled = bool(enabled_raw)
        if not enabled:
            continue

        project = _string(item.get("project", ""))
        account = _string(item.get("account", ""))
        wechat_id = _string(item.get("wechat_id", ""))
        confidence = _string(item.get("confidence", ""))
        aliases_raw = item.get("aliases", [])
        if not isinstance(aliases_raw, list):
            raise ValueError("aliases must be a JSON array")
        aliases = tuple(
            _string(value) for value in aliases_raw if _string(value)
        )

        if not project or not account:
            raise ValueError("project and account names must not be empty")
        if confidence != "high":
            raise ValueError("all enabled account mappings must have high confidence")
        if project in project_names:
            raise ValueError("duplicate project name")
        if account in account_names:
            raise ValueError("duplicate account name")

        project_names.add(project)
        account_names.add(account)
        projects.append(
            ProjectAccount(
                project=project,
                account=account,
                wechat_id=wechat_id,
                confidence=confidence,
                enabled=enabled,
                aliases=aliases,
            )
        )

    return tuple(projects)
