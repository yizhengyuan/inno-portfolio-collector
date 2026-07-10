from __future__ import annotations

import json
from pathlib import Path

from .models import ProjectAccount


def load_projects(path: Path) -> tuple[ProjectAccount, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("projects config must be a JSON array")

    projects: list[ProjectAccount] = []
    project_names: set[str] = set()
    account_names: set[str] = set()

    for item in payload:
        if not item.get("enabled", True):
            continue

        project = item.get("project", "").strip()
        account = item.get("account", "").strip()
        wechat_id = item.get("wechat_id", "").strip()
        confidence = item.get("confidence", "").strip()
        aliases = tuple(
            alias.strip() for alias in item.get("aliases", ()) if alias.strip()
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
                enabled=True,
                aliases=aliases,
            )
        )

    return tuple(projects)
