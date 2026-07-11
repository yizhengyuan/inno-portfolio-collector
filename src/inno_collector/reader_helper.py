from __future__ import annotations

import json
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
        return {"role": "reader", "vault_exists": False}
    if not isinstance(value, str) or not value:
        raise ValueError("invalid vault")
    vault = Path(value)
    if not vault.exists():
        return {"role": "reader", "vault_exists": False}
    return {"role": "reader", "vault_exists": True, "report": lint_vault(vault)}


def _preview_update(arguments: dict[str, object]) -> dict[str, object]:
    from .update_package import _read_update_package

    manifest, _target, payloads = _read_update_package(_path(arguments, "package"))
    return {
        "kind": manifest["kind"],
        "base_version": manifest["base_version"],
        "target_version": manifest["target_version"],
        "included": sorted(payloads),
        "deleted": manifest["deleted"],
    }


def _apply_update(arguments: dict[str, object]) -> dict[str, object]:
    from .update_package import apply_update_package

    return asdict(
        apply_update_package(
            _path(arguments, "package"),
            _path(arguments, "vault"),
        )
    )


def _create_draft(arguments: dict[str, object]) -> dict[str, object]:
    from .draft_package import _METADATA_FIELDS, _metadata
    from .vault import _atomic_write

    vault = _path(arguments, "vault").resolve()
    metadata_payload = {field: arguments.get(field) for field in _METADATA_FIELDS}
    metadata = _metadata(metadata_payload)
    body = arguments.get("body", "")
    kind = arguments.get("kind", "edit")
    if not isinstance(body, str) or kind not in {"note", "summary", "pitch", "edit"}:
        raise ValueError("invalid draft content")
    destination = vault / "10-编辑稿" / f"{metadata.draft_id}.md"
    if destination.exists():
        raise ValueError("draft already exists")
    fields = {
        "draft_id": metadata.draft_id,
        "draft_version": metadata.draft_version,
        "author": metadata.author,
        "title": metadata.title,
        "updated_at": metadata.updated_at,
        "source_ids": list(metadata.source_ids),
    }
    frontmatter = "\n".join(
        f"{name}: {json.dumps(value, ensure_ascii=False)}" for name, value in fields.items()
    )
    payload = f"---\n{frontmatter}\n---\n\n<!-- kind: {kind} -->\n\n{body.rstrip()}\n"
    _atomic_write(destination, payload.encode("utf-8"))
    return {"draft_path": str(destination), "draft_id": metadata.draft_id}


def _build_drafts(arguments: dict[str, object]) -> dict[str, object]:
    from .draft_package import build_draft_package

    paths = arguments.get("draft_paths")
    exported_at = arguments.get("exported_at")
    if not isinstance(paths, list) or any(not isinstance(value, str) for value in paths):
        raise ValueError("invalid draft paths")
    if not isinstance(exported_at, str):
        raise ValueError("invalid exported_at")
    return build_draft_package(
        _path(arguments, "vault"),
        paths,
        _path(arguments, "output"),
        exported_at=exported_at,
    )


def _rebuild_dashboard(arguments: dict[str, object]) -> dict[str, object]:
    from .dashboard import build_dashboard

    output = build_dashboard(_path(arguments, "vault"))
    return {"dashboard_path": str(output)}


HANDLERS: dict[str, Handler] = {
    "status": _status,
    "preview_update": _preview_update,
    "apply_update": _apply_update,
    "create_draft": _create_draft,
    "build_drafts": _build_drafts,
    "rebuild_dashboard": _rebuild_dashboard,
}


def main() -> int:
    return run_helper(HANDLERS, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
