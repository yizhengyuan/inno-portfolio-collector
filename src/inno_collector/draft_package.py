from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .ingest import markdown_image_destinations
from .package import _frontmatter, _open_regular, _safe_relative, lint_vault
from .update_package import _write_member, _zip_member_is_regular
from .vault import _atomic_write


class DraftPackageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DraftMetadata:
    draft_id: str
    draft_version: int
    author: str
    title: str
    updated_at: str
    source_ids: tuple[str, ...]


_DRAFT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
_SOURCE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_METADATA_FIELDS = {
    "draft_id",
    "draft_version",
    "author",
    "title",
    "updated_at",
    "source_ids",
}
_DRAFT_ROW_FIELDS = _METADATA_FIELDS | {"path", "size", "sha256", "attachments"}
_FILE_ROW_FIELDS = {"path", "size", "sha256"}
_MANIFEST_FIELDS = {"format_version", "exported_at", "drafts"}
_MAX_FILE_BYTES = 128 * 1024 * 1024


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftPackageError("invalid draft timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise DraftPackageError("invalid draft timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DraftPackageError("invalid draft timestamp")
    return value


def _metadata(payload: object) -> DraftMetadata:
    if not isinstance(payload, dict) or not _METADATA_FIELDS.issubset(payload):
        raise DraftPackageError("invalid draft metadata")
    draft_id = payload.get("draft_id")
    version = payload.get("draft_version")
    author = payload.get("author")
    title = payload.get("title")
    source_ids = payload.get("source_ids")
    if not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id):
        raise DraftPackageError("invalid draft id")
    if type(version) is not int or version <= 0:
        raise DraftPackageError("invalid draft version")
    if not isinstance(author, str) or not author.strip() or len(author) > 256:
        raise DraftPackageError("invalid draft author")
    if not isinstance(title, str) or not title.strip() or len(title) > 512:
        raise DraftPackageError("invalid draft title")
    if (
        not isinstance(source_ids, list)
        or any(not isinstance(value, str) or not _SOURCE_ID.fullmatch(value) for value in source_ids)
        or len(source_ids) != len(set(source_ids))
    ):
        raise DraftPackageError("invalid draft source ids")
    return DraftMetadata(
        draft_id=draft_id,
        draft_version=version,
        author=author,
        title=title,
        updated_at=_timestamp(payload.get("updated_at")),
        source_ids=tuple(source_ids),
    )


def _selected_draft(vault: Path, value: object) -> tuple[PurePosixPath, Path]:
    relative = _safe_relative(value)
    if (
        relative is None
        or len(relative.parts) < 2
        or relative.parts[0] != "10-编辑稿"
        or relative.parts[1] == "附件"
        or relative.suffix.casefold() != ".md"
    ):
        raise DraftPackageError("invalid selected draft path")
    path = vault.joinpath(*relative.parts)
    try:
        path.resolve(strict=True).relative_to(vault.resolve())
        details = path.lstat()
    except (OSError, ValueError):
        raise DraftPackageError("selected draft is unavailable") from None
    if not stat.S_ISREG(details.st_mode):
        raise DraftPackageError("selected draft is not a regular file")
    return relative, path


def _attachments(vault: Path, draft_path: Path, metadata: DraftMetadata, text: str) -> list[Path]:
    expected_root = (vault / "10-编辑稿" / "附件" / metadata.draft_id).resolve()
    result: list[Path] = []
    for _start, _end, raw in markdown_image_destinations(text):
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc:
            continue
        decoded = unquote(parsed.path)
        candidate = (draft_path.parent / decoded).resolve()
        try:
            candidate.relative_to(expected_root)
            details = candidate.lstat()
        except (OSError, ValueError):
            raise DraftPackageError("unsafe draft attachment") from None
        if not stat.S_ISREG(details.st_mode) or candidate.suffix.casefold() not in _IMAGE_EXTENSIONS:
            raise DraftPackageError("invalid draft attachment")
        if candidate not in result:
            result.append(candidate)
    return result


def _file_row(root: Path, path: Path) -> tuple[dict[str, object], bytes]:
    try:
        payload = _open_regular(path, max_bytes=_MAX_FILE_BYTES)
    except OSError:
        raise DraftPackageError("unable to read draft file") from None
    return (
        {
            "path": path.relative_to(root).as_posix(),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        payload,
    )


def build_draft_package(
    vault: Path,
    draft_paths: list[str],
    output: Path,
    *,
    exported_at: str,
) -> dict[str, object]:
    root = Path(vault).resolve()
    if not draft_paths or len(draft_paths) != len(set(draft_paths)):
        raise DraftPackageError("draft selection must be unique and nonempty")
    if lint_vault(root)["errors"]:
        raise DraftPackageError("Vault validation failed")
    exported = _timestamp(exported_at)
    draft_rows: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    for raw_path in draft_paths:
        relative, path = _selected_draft(root, raw_path)
        frontmatter = _frontmatter(path)
        if not isinstance(frontmatter, dict) or set(frontmatter) != _METADATA_FIELDS:
            raise DraftPackageError("invalid draft frontmatter fields")
        metadata = _metadata(frontmatter)
        draft_row, draft_bytes = _file_row(root, path)
        text = draft_bytes.decode("utf-8")
        attachment_rows: list[dict[str, object]] = []
        attachment_payloads: list[tuple[str, bytes]] = []
        for attachment in _attachments(root, path, metadata, text):
            row, data = _file_row(root, attachment)
            attachment_rows.append(row)
            attachment_payloads.append((str(row["path"]), data))
        draft_rows.append(
            {
                **asdict(metadata),
                "source_ids": list(metadata.source_ids),
                **draft_row,
                "attachments": attachment_rows,
            }
        )
        payloads.append((relative.as_posix(), draft_bytes))
        payloads.extend(attachment_payloads)

    manifest = {"format_version": 1, "exported_at": exported, "drafts": draft_rows}
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    installed = False
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".inno-drafts-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w") as archive:
            _write_member(
                archive,
                "draft-manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
            )
            for relative, data in payloads:
                _write_member(archive, f"payload/{relative}", data)
        _read_draft_archive(temporary)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except OSError:
            raise DraftPackageError("draft output already exists or was claimed") from None
        installed = True
        digest = hashlib.sha256(_open_regular(destination)).hexdigest()
        return {
            "package_path": str(destination),
            "package_sha256": digest,
            "draft_count": len(draft_rows),
        }
    except BaseException:
        if installed:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_file_row(payload: object, *, draft_id: str | None = None) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _FILE_ROW_FIELDS:
        raise DraftPackageError("invalid draft file row")
    relative = _safe_relative(payload.get("path"))
    size = payload.get("size")
    digest = payload.get("sha256")
    if relative is None or type(size) is not int or size < 0:
        raise DraftPackageError("invalid draft file row")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DraftPackageError("invalid draft file hash")
    if draft_id is not None and (
        len(relative.parts) < 4
        or relative.parts[:3] != ("10-编辑稿", "附件", draft_id)
        or relative.suffix.casefold() not in _IMAGE_EXTENSIONS
    ):
        raise DraftPackageError("invalid draft attachment path")
    return payload


def _validate_manifest(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise DraftPackageError("invalid draft manifest")
    if type(payload.get("format_version")) is not int or payload["format_version"] != 1:
        raise DraftPackageError("unsupported draft manifest version")
    _timestamp(payload.get("exported_at"))
    drafts = payload.get("drafts")
    if not isinstance(drafts, list) or not drafts:
        raise DraftPackageError("invalid draft manifest rows")
    ids: set[tuple[str, int]] = set()
    for row in drafts:
        if not isinstance(row, dict) or set(row) != _DRAFT_ROW_FIELDS:
            raise DraftPackageError("invalid draft row")
        metadata = _metadata(row)
        identity = (metadata.draft_id, metadata.draft_version)
        if identity in ids:
            raise DraftPackageError("duplicate draft identity")
        ids.add(identity)
        draft_file = _validate_file_row({field: row[field] for field in _FILE_ROW_FIELDS})
        relative = _safe_relative(draft_file["path"])
        if relative is None or relative.parts[0] != "10-编辑稿" or relative.suffix.casefold() != ".md":
            raise DraftPackageError("invalid draft path")
        attachments = row.get("attachments")
        if not isinstance(attachments, list):
            raise DraftPackageError("invalid draft attachments")
        for attachment in attachments:
            _validate_file_row(attachment, draft_id=metadata.draft_id)
    return payload


def _read_draft_archive(path: Path) -> tuple[dict[str, object], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or "draft-manifest.json" not in names:
                raise DraftPackageError("invalid draft package members")
            if any(
                "\\" in name
                or name.startswith("/")
                or ".." in PurePosixPath(name).parts
                or not _zip_member_is_regular(info)
                or info.file_size > _MAX_FILE_BYTES
                for name, info in zip(names, infos, strict=True)
            ):
                raise DraftPackageError("unsafe draft package member")
            try:
                manifest = _validate_manifest(json.loads(archive.read("draft-manifest.json")))
            except (UnicodeError, json.JSONDecodeError):
                raise DraftPackageError("invalid draft manifest JSON") from None
            expected: dict[str, dict[str, object]] = {}
            for row in manifest["drafts"]:
                expected[str(row["path"])] = row
                for attachment in row["attachments"]:
                    expected[str(attachment["path"])] = attachment
            payloads: dict[str, bytes] = {}
            for info in infos:
                if info.filename == "draft-manifest.json":
                    continue
                if not info.filename.startswith("payload/"):
                    raise DraftPackageError("unexpected draft package member")
                relative = info.filename.removeprefix("payload/")
                row = expected.get(relative)
                if row is None or relative in payloads:
                    raise DraftPackageError("undeclared draft payload")
                data = archive.read(info)
                if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise DraftPackageError("draft payload hash mismatch")
                payloads[relative] = data
            if set(payloads) != set(expected):
                raise DraftPackageError("draft package is incomplete")
            return manifest, payloads
    except (OSError, zipfile.BadZipFile):
        raise DraftPackageError("unable to read draft package") from None


def receive_draft_package(package_path: Path, inbox: Path) -> dict[str, object]:
    package = Path(package_path)
    manifest, payloads = _read_draft_archive(package)
    digest = hashlib.sha256(_open_regular(package)).hexdigest()
    root = Path(inbox)
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / digest
    if receipt.exists():
        return {"receipt_path": str(receipt), "draft_count": len(manifest["drafts"]), "existing": True}
    stage = Path(tempfile.mkdtemp(prefix=".draft-receipt-", dir=root))
    try:
        (stage / "draft-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for relative, data in payloads.items():
            destination = stage / "payload" / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        try:
            os.replace(stage, receipt)
        except OSError:
            if not receipt.is_dir():
                raise DraftPackageError("unable to install draft receipt") from None
        return {"receipt_path": str(receipt), "draft_count": len(manifest["drafts"]), "existing": False}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _read_receipt(receipt: Path) -> tuple[dict[str, object], dict[str, bytes]]:
    try:
        manifest = _validate_manifest(
            json.loads((receipt / "draft-manifest.json").read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DraftPackageError("invalid draft receipt") from None
    payloads: dict[str, bytes] = {}
    for row in manifest["drafts"]:
        for item in [row, *row["attachments"]]:
            relative = str(item["path"])
            try:
                data = _open_regular(receipt / "payload" / Path(relative), max_bytes=_MAX_FILE_BYTES)
            except OSError:
                raise DraftPackageError("invalid draft receipt payload") from None
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise DraftPackageError("draft receipt hash mismatch")
            payloads[relative] = data
    return manifest, payloads


def accept_received_draft(receipt: Path, vault: Path) -> dict[str, object]:
    manifest, payloads = _read_receipt(Path(receipt))
    root = Path(vault).resolve()
    drafts_root = root / "10-编辑稿"
    drafts_root.mkdir(parents=True, exist_ok=True)
    existing: dict[str, list[tuple[Path, DraftMetadata, str]]] = {}
    for path in drafts_root.rglob("*.md"):
        frontmatter = _frontmatter(path)
        try:
            metadata = _metadata(frontmatter)
        except DraftPackageError:
            continue
        digest = hashlib.sha256(_open_regular(path)).hexdigest()
        existing.setdefault(metadata.draft_id, []).append((path, metadata, digest))

    for row in manifest["drafts"]:
        for attachment in row["attachments"]:
            relative = str(attachment["path"])
            target = root.joinpath(*PurePosixPath(relative).parts)
            if target.exists() and _open_regular(target) != payloads[relative]:
                raise DraftPackageError("conflicting draft attachment")

    created = unchanged = conflicts = 0
    for row in manifest["drafts"]:
        metadata = _metadata(row)
        data = payloads[str(row["path"])]
        digest = hashlib.sha256(data).hexdigest()
        matches = existing.get(metadata.draft_id, [])
        if any(item[1].draft_version == metadata.draft_version and item[2] == digest for item in matches):
            unchanged += 1
            continue
        original = root.joinpath(*PurePosixPath(str(row["path"])).parts)
        if matches or original.exists():
            destination = original.with_name(f"{original.stem}-conflict-{digest[:12]}.md")
            counter = 2
            while destination.exists():
                destination = original.with_name(
                    f"{original.stem}-conflict-{digest[:12]}-{counter}.md"
                )
                counter += 1
            conflicts += 1
        else:
            destination = original
            created += 1
        _atomic_write(destination, data)
        for attachment in row["attachments"]:
            relative = str(attachment["path"])
            target = root.joinpath(*PurePosixPath(relative).parts)
            attachment_data = payloads[relative]
            if not target.exists():
                _atomic_write(target, attachment_data)
        existing.setdefault(metadata.draft_id, []).append((destination, metadata, digest))
    return {
        "created": created,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "draft_count": len(manifest["drafts"]),
    }
