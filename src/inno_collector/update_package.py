from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .content_manifest import (
    ContentFile,
    ContentManifest,
    ContentManifestError,
    _is_content_path,
    build_content_manifest,
    parse_content_manifest,
)
from .package import _open_regular, _safe_relative, lint_vault


class UpdatePackageError(ValueError):
    pass


_FORMAT_VERSION = 1
_MANIFEST_FIELDS = {
    "format_version",
    "kind",
    "created_at",
    "base_version",
    "target_version",
    "files",
    "deleted",
}
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_MEMBER_BYTES = 128 * 1024 * 1024


def _manifest_payload(
    target: ContentManifest,
    *,
    kind: str,
    base_version: str | None,
    deleted: list[str],
) -> dict[str, object]:
    return {
        "format_version": _FORMAT_VERSION,
        "kind": kind,
        "created_at": target.created_at,
        "base_version": base_version,
        "target_version": target.content_version,
        "files": [asdict(row) for row in target.files],
        "deleted": deleted,
    }


def _parse_update_manifest(payload: object) -> tuple[dict[str, object], ContentManifest]:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise UpdatePackageError("invalid update manifest")
    version = payload.get("format_version")
    if type(version) is not int or version != _FORMAT_VERSION:
        raise UpdatePackageError("unsupported update manifest version")
    kind = payload.get("kind")
    base_version = payload.get("base_version")
    if kind == "baseline":
        if base_version is not None:
            raise UpdatePackageError("baseline package has a base version")
    elif kind == "incremental":
        if not isinstance(base_version, str):
            raise UpdatePackageError("incremental package lacks a base version")
    else:
        raise UpdatePackageError("invalid update package kind")

    try:
        target = parse_content_manifest(
            {
                "format_version": version,
                "created_at": payload.get("created_at"),
                "content_version": payload.get("target_version"),
                "files": payload.get("files"),
            }
        )
    except ContentManifestError as exc:
        raise UpdatePackageError(str(exc)) from None

    raw_deleted = payload.get("deleted")
    if not isinstance(raw_deleted, list):
        raise UpdatePackageError("invalid deleted paths")
    deleted: list[str] = []
    target_paths = {row.path for row in target.files}
    for value in raw_deleted:
        relative = _safe_relative(value)
        if (
            relative is None
            or not isinstance(value, str)
            or not _is_content_path(relative)
            or value in target_paths
            or value in deleted
        ):
            raise UpdatePackageError("invalid deleted path")
        deleted.append(value)
    if deleted != sorted(deleted):
        raise UpdatePackageError("deleted paths are not sorted")
    if kind == "baseline" and deleted:
        raise UpdatePackageError("baseline package deletes files")
    return payload, target


def _zip_member_is_regular(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    return not info.is_dir() and file_type in {0, stat.S_IFREG}


def _read_update_package(
    path: Path,
    *,
    expected_payload: set[str] | None = None,
) -> tuple[dict[str, object], ContentManifest, set[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or "update-manifest.json" not in names:
                raise UpdatePackageError("invalid update package members")
            if any(
                "\\" in name
                or name.startswith("/")
                or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
                or not _zip_member_is_regular(info)
                or info.file_size > _MAX_MEMBER_BYTES
                for name, info in zip(names, infos, strict=True)
            ):
                raise UpdatePackageError("unsafe update package member")
            manifest_info = archive.getinfo("update-manifest.json")
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise UpdatePackageError("update manifest is too large")
            try:
                raw_manifest = json.loads(archive.read(manifest_info))
            except (UnicodeError, json.JSONDecodeError):
                raise UpdatePackageError("invalid update manifest JSON") from None
            manifest, target = _parse_update_manifest(raw_manifest)
            target_by_path = {row.path: row for row in target.files}
            payload_paths: set[str] = set()
            for info in infos:
                if info.filename == "update-manifest.json":
                    continue
                if not info.filename.startswith("payload/"):
                    raise UpdatePackageError("unexpected update package member")
                relative = info.filename.removeprefix("payload/")
                row = target_by_path.get(relative)
                if row is None or relative in payload_paths:
                    raise UpdatePackageError("undeclared update payload")
                data = archive.read(info)
                if len(data) != row.size or hashlib.sha256(data).hexdigest() != row.sha256:
                    raise UpdatePackageError("update payload hash mismatch")
                payload_paths.add(relative)
            if manifest["kind"] == "baseline" and payload_paths != set(target_by_path):
                raise UpdatePackageError("baseline package is incomplete")
            if expected_payload is not None and payload_paths != expected_payload:
                raise UpdatePackageError("update package payload mismatch")
            return manifest, target, payload_paths
    except (OSError, zipfile.BadZipFile):
        raise UpdatePackageError("unable to read update package") from None


def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build_update_package(
    vault: Path,
    output: Path,
    *,
    base_package: Path | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    root = Path(vault).resolve()
    report = lint_vault(root)
    if report["errors"]:
        raise UpdatePackageError("Vault validation failed")
    created = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    target = build_content_manifest(root, created_at=created)
    target_by_path = {row.path: row for row in target.files}

    base_version: str | None = None
    base_by_path: dict[str, ContentFile] = {}
    if base_package is not None:
        _base_manifest, base_target, _payload = _read_update_package(Path(base_package))
        base_version = base_target.content_version
        base_by_path = {row.path: row for row in base_target.files}

    included = sorted(
        path
        for path, row in target_by_path.items()
        if path not in base_by_path or base_by_path[path] != row
    )
    deleted = sorted(path for path in base_by_path if path not in target_by_path)
    kind = "baseline" if base_version is None else "incremental"
    manifest = _manifest_payload(
        target,
        kind=kind,
        base_version=base_version,
        deleted=deleted,
    )

    destination = Path(output)
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError:
        pass
    else:
        raise UpdatePackageError("update output must be outside Vault")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    installed = False
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".inno-update-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w") as archive:
            encoded = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            _write_member(archive, "update-manifest.json", encoded)
            for relative in included:
                path = root.joinpath(*PurePosixPath(relative).parts)
                try:
                    data = _open_regular(path, max_bytes=_MAX_MEMBER_BYTES)
                except OSError:
                    raise UpdatePackageError("unable to read update payload") from None
                row = target_by_path[relative]
                if len(data) != row.size or hashlib.sha256(data).hexdigest() != row.sha256:
                    raise UpdatePackageError("content changed during package build")
                _write_member(archive, f"payload/{relative}", data)

        current = build_content_manifest(root, created_at=created)
        if current.content_version != target.content_version:
            raise UpdatePackageError("content changed during package build")
        _read_update_package(temporary, expected_payload=set(included))
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except OSError:
            raise UpdatePackageError("update output already exists or was claimed") from None
        installed = True
        digest = hashlib.sha256(_open_regular(destination)).hexdigest()
        return {
            "package_path": str(destination),
            "kind": kind,
            "base_version": base_version,
            "target_version": target.content_version,
            "included": included,
            "deleted": deleted,
            "package_sha256": digest,
        }
    except BaseException:
        if installed:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
