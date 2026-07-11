from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .package import _inventory, _open_regular, _safe_relative


class ContentManifestError(ValueError):
    pass


_FORMAT_VERSION = 1
_ROW_FIELDS = {"path", "size", "sha256"}
_MANIFEST_FIELDS = {"format_version", "created_at", "content_version", "files"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_VERSION = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROOT_FILES = {"00-首页.md", "01-采集状态.md"}
_CONTENT_ROOTS = {"02-项目", "03-文章", "04-附件", "80-离线看板", "90-系统"}
_IGNORED_FILES = {"90-系统/manifest.json.lock"}


@dataclass(frozen=True, slots=True)
class ContentFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ContentManifest:
    format_version: int
    created_at: str
    content_version: str
    files: tuple[ContentFile, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "created_at": self.created_at,
            "content_version": self.content_version,
            "files": [asdict(row) for row in self.files],
        }


def _validate_created_at(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentManifestError("invalid content manifest created_at")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ContentManifestError("invalid content manifest created_at") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContentManifestError("invalid content manifest created_at")
    return value


def _is_content_path(path: PurePosixPath) -> bool:
    text = path.as_posix()
    if text in _IGNORED_FILES:
        return False
    if text in _ROOT_FILES:
        return True
    return len(path.parts) >= 2 and path.parts[0] in _CONTENT_ROOTS


def _canonical_rows(files: tuple[ContentFile, ...]) -> bytes:
    rows = [asdict(row) for row in files]
    return json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_version(files: tuple[ContentFile, ...]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_rows(files)).hexdigest()


def build_content_manifest(vault: Path, *, created_at: str) -> ContentManifest:
    created = _validate_created_at(created_at)
    root = Path(vault)
    inventory, _directories, errors, _fingerprints = _inventory(root)
    if errors:
        raise ContentManifestError("unsafe Vault inventory")

    rows: list[ContentFile] = []
    for relative in sorted(inventory, key=lambda value: value.as_posix()):
        if not _is_content_path(relative):
            continue
        path_text = relative.as_posix()
        if unicodedata.normalize("NFC", path_text) != path_text:
            raise ContentManifestError("content path is not NFC normalized")
        try:
            payload = _open_regular(root.joinpath(*relative.parts))
        except OSError:
            raise ContentManifestError("unable to read content file") from None
        rows.append(
            ContentFile(
                path=path_text,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )

    files = tuple(rows)
    return ContentManifest(
        format_version=_FORMAT_VERSION,
        created_at=created,
        content_version=_content_version(files),
        files=files,
    )


def parse_content_manifest(payload: object) -> ContentManifest:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise ContentManifestError("invalid content manifest object")
    version = payload.get("format_version")
    if type(version) is not int or version != _FORMAT_VERSION:
        raise ContentManifestError("unsupported content manifest version")
    created_at = _validate_created_at(payload.get("created_at"))
    raw_version = payload.get("content_version")
    if not isinstance(raw_version, str) or not _CONTENT_VERSION.fullmatch(raw_version):
        raise ContentManifestError("invalid content version")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ContentManifestError("invalid content manifest files")

    rows: list[ContentFile] = []
    seen: set[str] = set()
    for raw_row in raw_files:
        if not isinstance(raw_row, dict) or set(raw_row) != _ROW_FIELDS:
            raise ContentManifestError("invalid content manifest file row")
        path_value = raw_row.get("path")
        path = _safe_relative(path_value)
        if (
            path is None
            or not isinstance(path_value, str)
            or unicodedata.normalize("NFC", path_value) != path_value
            or not _is_content_path(path)
            or path_value in seen
        ):
            raise ContentManifestError("invalid content manifest path")
        size = raw_row.get("size")
        if type(size) is not int or size < 0:
            raise ContentManifestError("invalid content manifest size")
        digest = raw_row.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ContentManifestError("invalid content manifest hash")
        seen.add(path_value)
        rows.append(ContentFile(path=path_value, size=size, sha256=digest))

    files = tuple(rows)
    if tuple(row.path for row in files) != tuple(sorted(row.path for row in files)):
        raise ContentManifestError("content manifest files are not sorted")
    if _content_version(files) != raw_version:
        raise ContentManifestError("content manifest version mismatch")
    return ContentManifest(
        format_version=version,
        created_at=created_at,
        content_version=raw_version,
        files=files,
    )
