from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from .helper_protocol import Handler, run_helper


def _path(arguments: dict[str, object], name: str) -> Path:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return Path(value)


def _status(arguments: dict[str, object]) -> dict[str, object]:
    from .package import lint_vault

    value = arguments.get("vault")
    if value is None:
        return {"role": "collector", "vault_exists": False}
    if not isinstance(value, str) or not value:
        raise ValueError("invalid vault")
    vault = Path(value)
    if not vault.exists():
        return {"role": "collector", "vault_exists": False}
    return {"role": "collector", "vault_exists": True, "report": lint_vault(vault)}


def _collect(arguments: dict[str, object]) -> dict[str, object]:
    from .config import load_projects
    from .exporter import MooreExporterAdapter
    from .pipeline import CollectionPipeline

    projects_path = _path(arguments, "projects")
    runtime = _path(arguments, "runtime")
    exporter_runtime = _path(arguments, "exporter_runtime")
    since = arguments.get("since", "2026-01-01")
    dry_run = arguments.get("dry_run", False)
    if not isinstance(since, str) or type(dry_run) is not bool:
        raise ValueError("invalid collect arguments")
    executable = arguments.get("exporter_executable")
    if executable is None and getattr(sys, "frozen", False):
        executable = str(Path(sys.executable).with_name("MooreExporterHelper"))
    if executable is not None:
        if not isinstance(executable, str) or not executable:
            raise ValueError("invalid exporter executable")
        backend = MooreExporterAdapter(
            Path("unused-in-frozen-mode"),
            exporter_runtime,
            command_prefix=(executable,),
        )
    else:
        backend = MooreExporterAdapter(
            _path(arguments, "exporter_script"),
            exporter_runtime,
        )
    result = CollectionPipeline(backend, runtime_dir=runtime).run(
        load_projects(projects_path),
        since=since,
        dry_run=dry_run,
    )
    return asdict(result)


def _build_update(arguments: dict[str, object]) -> dict[str, object]:
    from .update_package import build_update_package

    base = arguments.get("base_package")
    created_at = arguments.get("created_at")
    if base is not None and not isinstance(base, str):
        raise ValueError("invalid base package")
    if created_at is not None and not isinstance(created_at, str):
        raise ValueError("invalid created_at")
    return build_update_package(
        _path(arguments, "vault"),
        _path(arguments, "output"),
        base_package=Path(base) if base else None,
        created_at=created_at,
    )


def _receive_drafts(arguments: dict[str, object]) -> dict[str, object]:
    from .draft_package import receive_draft_package

    return receive_draft_package(
        _path(arguments, "package"),
        _path(arguments, "inbox"),
    )


def _list_received_drafts(arguments: dict[str, object]) -> dict[str, object]:
    from .draft_package import list_received_drafts

    return list_received_drafts(_path(arguments, "inbox"))


def _accept_draft(arguments: dict[str, object]) -> dict[str, object]:
    from .draft_package import accept_received_draft

    return accept_received_draft(
        _path(arguments, "receipt"),
        _path(arguments, "vault"),
    )


HANDLERS: dict[str, Handler] = {
    "status": _status,
    "collect": _collect,
    "build_update": _build_update,
    "receive_drafts": _receive_drafts,
    "list_received_drafts": _list_received_drafts,
    "accept_draft": _accept_draft,
}


def main() -> int:
    return run_helper(HANDLERS, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
