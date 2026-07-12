from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import struct
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..diagnostics import sanitize_diagnostic
from ..draft_package import (
    DraftPackageError,
    _read_receipt,
    accept_received_draft,
    receive_draft_package,
)
from .security import MAX_UPLOAD_BYTES, MAX_UPLOAD_FILE_BYTES


MAX_DRAFT_UPLOAD_BYTES = MAX_UPLOAD_BYTES
MAX_DRAFT_FILE_BYTES = MAX_UPLOAD_FILE_BYTES
MAX_DRAFT_UPLOAD_FILES = 1
MAX_DRAFT_PREVIEWS = 128
MAX_DRAFT_PREVIEW_TTL_SECONDS = 24 * 60 * 60
MAX_DRAFT_ARCHIVE_MEMBERS = 1024
MAX_DRAFT_ARCHIVE_UNCOMPRESSED_BYTES = 128 << 20
_DEFAULT_PREVIEW_TTL_SECONDS = 60 * 60
_DEFAULT_MAX_PREVIEWS = 32
_RECEIPT_NAME = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_CHUNK_BYTES = 64 << 10
_ZIP_EOCD = b"PK\x05\x06"
_ZIP_EOCD_BYTES = 22
_ZIP_MAX_COMMENT_BYTES = (1 << 16) - 1


UploadPayload = Path | bytes | bytearray | memoryview | BinaryIO | Iterable[bytes]
Receiver = Callable[[Path, Path], dict[str, object]]
Acceptor = Callable[[Path, Path], dict[str, object]]


class DraftUploadError(ValueError):
    """Stable error safe to expose through the loopback Web API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(slots=True)
class _PreviewRecord:
    receipt_id: str
    path: Path
    draft_count: int
    drafts: list[dict[str, object]]
    touched_at: float
    sequence: int


def _unsafe_inbox() -> DraftUploadError:
    return DraftUploadError(
        500,
        "unsafe_draft_inbox",
        "The local draft inbox is unavailable.",
    )


def _invalid_package() -> DraftUploadError:
    return DraftUploadError(
        422,
        "invalid_draft_package",
        "The selected file is not a valid draft package.",
    )


class DraftUploadManager:
    """Stages, previews, and accepts one bounded ``.inno-drafts`` upload.

    Receipt filesystem paths never cross this boundary.  Callers receive a
    short-lived random receipt id which is only meaningful to this manager.
    """

    def __init__(
        self,
        inbox_root: Path,
        *,
        max_file_bytes: int = MAX_DRAFT_FILE_BYTES,
        max_total_bytes: int = MAX_DRAFT_UPLOAD_BYTES,
        max_archive_members: int = MAX_DRAFT_ARCHIVE_MEMBERS,
        max_uncompressed_bytes: int = MAX_DRAFT_ARCHIVE_UNCOMPRESSED_BYTES,
        max_receipts: int = _DEFAULT_MAX_PREVIEWS,
        receipt_ttl_seconds: float = _DEFAULT_PREVIEW_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        receiver: Receiver = receive_draft_package,
        acceptor: Acceptor = accept_received_draft,
    ) -> None:
        if (
            type(max_file_bytes) is not int
            or not 1 <= max_file_bytes <= MAX_DRAFT_FILE_BYTES
        ):
            raise ValueError("max_file_bytes exceeds the hard upload limit")
        if (
            type(max_total_bytes) is not int
            or not 1 <= max_total_bytes <= MAX_DRAFT_UPLOAD_BYTES
        ):
            raise ValueError("max_total_bytes exceeds the hard upload limit")
        if type(max_receipts) is not int or not 1 <= max_receipts <= MAX_DRAFT_PREVIEWS:
            raise ValueError("max_receipts exceeds the hard preview limit")
        if (
            type(max_archive_members) is not int
            or not 1 <= max_archive_members <= MAX_DRAFT_ARCHIVE_MEMBERS
        ):
            raise ValueError("max_archive_members exceeds the hard archive limit")
        if (
            type(max_uncompressed_bytes) is not int
            or not 1
            <= max_uncompressed_bytes
            <= MAX_DRAFT_ARCHIVE_UNCOMPRESSED_BYTES
        ):
            raise ValueError("max_uncompressed_bytes exceeds the hard archive limit")
        if (
            isinstance(receipt_ttl_seconds, bool)
            or not isinstance(receipt_ttl_seconds, (int, float))
            or not 0 < receipt_ttl_seconds <= MAX_DRAFT_PREVIEW_TTL_SECONDS
        ):
            raise ValueError("receipt_ttl_seconds exceeds the hard preview limit")
        if not callable(clock) or not callable(receiver) or not callable(acceptor):
            raise TypeError("draft upload dependencies must be callable")

        candidate = Path(inbox_root).expanduser().absolute()
        self._prepare_directory(candidate)
        self.inbox_root = candidate
        self._canonical_inbox = candidate.resolve(strict=True)
        self.staging_root = candidate / ".uploads"
        self.receipts_root = candidate / "receipts"
        self._prepare_directory(self.staging_root)
        self._prepare_directory(self.receipts_root)
        self._canonical_staging = self.staging_root.resolve(strict=True)
        self._canonical_receipts = self.receipts_root.resolve(strict=True)
        try:
            self._canonical_staging.relative_to(self._canonical_inbox)
            self._canonical_receipts.relative_to(self._canonical_inbox)
        except ValueError:
            raise _unsafe_inbox() from None

        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_archive_members = max_archive_members
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.max_receipts = max_receipts
        self.receipt_ttl_seconds = float(receipt_ttl_seconds)
        self.clock = clock
        self._receiver = receiver
        self._acceptor = acceptor
        self._lock = threading.RLock()
        self._records: dict[str, _PreviewRecord] = {}
        self._receipt_ids_by_path: dict[Path, str] = {}
        self._sequence = 0

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        try:
            if path.exists() or path.is_symlink():
                details = path.lstat()
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                    raise _unsafe_inbox()
            else:
                path.mkdir(parents=True, mode=0o700)
            if path.is_symlink() or not path.is_dir():
                raise _unsafe_inbox()
        except DraftUploadError:
            raise
        except OSError:
            raise _unsafe_inbox() from None

    def _ensure_roots(self) -> None:
        try:
            for path, expected in (
                (self.inbox_root, self._canonical_inbox),
                (self.staging_root, self._canonical_staging),
                (self.receipts_root, self._canonical_receipts),
            ):
                details = path.lstat()
                if (
                    stat.S_ISLNK(details.st_mode)
                    or not stat.S_ISDIR(details.st_mode)
                    or path.resolve(strict=True) != expected
                ):
                    raise _unsafe_inbox()
        except DraftUploadError:
            raise
        except OSError:
            raise _unsafe_inbox() from None

    @staticmethod
    def _validate_filename(filename: object) -> str:
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or filename != Path(filename).name
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 for character in filename)
            or not filename.casefold().endswith(".inno-drafts")
        ):
            raise DraftUploadError(
                400,
                "invalid_draft_upload",
                "Select one .inno-drafts file.",
            )
        return filename

    @staticmethod
    def _chunks(payload: UploadPayload) -> Iterator[bytes]:
        if isinstance(payload, Path):
            descriptor: int | None = None
            try:
                details = payload.lstat()
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                    raise DraftUploadError(
                        400,
                        "invalid_draft_upload",
                        "The uploaded draft file could not be read.",
                    )
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(payload, flags)
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    os.close(descriptor)
                    descriptor = None
                    raise DraftUploadError(
                        400,
                        "invalid_draft_upload",
                        "The uploaded draft file could not be read.",
                    )
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = None
                    while True:
                        chunk = handle.read(_CHUNK_BYTES)
                        if not chunk:
                            return
                        yield chunk
                return
            except DraftUploadError:
                raise
            except OSError:
                raise DraftUploadError(
                    400,
                    "invalid_draft_upload",
                    "The uploaded draft file could not be read.",
                ) from None
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        if isinstance(payload, (bytes, bytearray, memoryview)):
            yield bytes(payload)
            return
        reader = getattr(payload, "read", None)
        if callable(reader):
            while True:
                chunk = reader(_CHUNK_BYTES)
                if chunk == b"":
                    return
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise DraftUploadError(
                        400,
                        "invalid_draft_upload",
                        "The uploaded draft file could not be read.",
                    )
                yield bytes(chunk)
        else:
            if isinstance(payload, str):
                raise DraftUploadError(
                    400,
                    "invalid_draft_upload",
                    "The uploaded draft file could not be read.",
                )
            try:
                iterator = iter(payload)
            except TypeError:
                raise DraftUploadError(
                    400,
                    "invalid_draft_upload",
                    "The uploaded draft file could not be read.",
                ) from None
            for chunk in iterator:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise DraftUploadError(
                        400,
                        "invalid_draft_upload",
                        "The uploaded draft file could not be read.",
                    )
                yield bytes(chunk)

    def _new_stage(self) -> Path:
        self._ensure_roots()
        try:
            stage = Path(tempfile.mkdtemp(prefix=".draft-upload-", dir=self.staging_root))
            details = stage.lstat()
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISDIR(details.st_mode)
                or stage.resolve(strict=True).parent != self._canonical_staging
            ):
                raise _unsafe_inbox()
            return stage
        except DraftUploadError:
            raise
        except OSError:
            raise _unsafe_inbox() from None

    @staticmethod
    def _remove_stage(stage: Path) -> None:
        try:
            if stage.is_symlink():
                stage.unlink(missing_ok=True)
            else:
                shutil.rmtree(stage, ignore_errors=True)
        except OSError:
            return

    def _write_upload(self, stage: Path, payload: UploadPayload) -> Path:
        destination = stage / "package.inno-drafts"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        total = 0
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in self._chunks(payload):
                    next_total = total + len(chunk)
                    if (
                        next_total > self.max_file_bytes
                        or next_total > self.max_total_bytes
                    ):
                        raise DraftUploadError(
                            413,
                            "upload_too_large",
                            "The draft upload exceeds the safe size limit.",
                        )
                    handle.write(chunk)
                    total = next_total
            if total == 0:
                raise _invalid_package()
            details = destination.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise _invalid_package()
            return destination
        except DraftUploadError:
            raise
        except (OSError, TypeError, ValueError):
            raise DraftUploadError(
                400,
                "invalid_draft_upload",
                "The uploaded draft file could not be read.",
            ) from None
        except Exception:
            raise DraftUploadError(
                400,
                "invalid_draft_upload",
                "The uploaded draft file could not be read.",
            ) from None

    def _validate_archive_budget(self, path: Path) -> None:
        """Reject oversized central directories before any member is extracted."""

        try:
            file_size = path.stat().st_size
            tail_bytes = min(file_size, _ZIP_EOCD_BYTES + _ZIP_MAX_COMMENT_BYTES)
            with path.open("rb") as handle:
                handle.seek(file_size - tail_bytes)
                tail = handle.read(tail_bytes)
            marker = tail.rfind(_ZIP_EOCD)
            if marker < 0 or len(tail) - marker < _ZIP_EOCD_BYTES:
                raise _invalid_package()
            (
                _signature,
                disk_number,
                directory_disk,
                disk_members,
                total_members,
                directory_size,
                directory_offset,
                comment_size,
            ) = struct.unpack_from("<4s4H2LH", tail, marker)
            eocd_offset = file_size - tail_bytes + marker
            if (
                disk_number != 0
                or directory_disk != 0
                or disk_members != total_members
                or not 1 <= total_members <= self.max_archive_members
                or total_members == 0xFFFF
                or directory_size == 0xFFFFFFFF
                or directory_offset == 0xFFFFFFFF
                or marker + _ZIP_EOCD_BYTES + comment_size != len(tail)
                or directory_offset + directory_size > eocd_offset
            ):
                raise _invalid_package()
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) != total_members:
                    raise _invalid_package()
                total = 0
                for info in infos:
                    if info.file_size < 0 or info.compress_size < 0:
                        raise _invalid_package()
                    total += info.file_size
                    if total > self.max_uncompressed_bytes:
                        raise _invalid_package()
                    if info.file_size > max(1, info.compress_size) * 1000:
                        raise _invalid_package()
        except DraftUploadError:
            raise
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise _invalid_package() from None
        except Exception:
            raise _invalid_package() from None

    def _safe_receipt_path(self, raw_path: object) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise _invalid_package()
        path = Path(raw_path)
        try:
            details = path.lstat()
            resolved = path.resolve(strict=True)
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISDIR(details.st_mode)
                or not _RECEIPT_NAME.fullmatch(path.name)
                or resolved.parent != self._canonical_receipts
            ):
                raise _invalid_package()
            return resolved
        except DraftUploadError:
            raise
        except (OSError, ValueError):
            raise _invalid_package() from None

    @staticmethod
    def _safe_drafts(path: Path) -> tuple[int, list[dict[str, object]]]:
        try:
            manifest, _payloads = _read_receipt(path)
            rows = manifest.get("drafts")
            if not isinstance(rows, list) or not rows:
                raise _invalid_package()
            drafts: list[dict[str, object]] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise _invalid_package()
                source_ids = row.get("source_ids")
                attachments = row.get("attachments")
                drafts.append(
                    {
                        "draft_id": str(row.get("draft_id") or "")[:64],
                        "draft_version": row.get("draft_version"),
                        "author": sanitize_diagnostic(row.get("author"), fallback="")[:256],
                        "title": sanitize_diagnostic(row.get("title"), fallback="")[:512],
                        "updated_at": str(row.get("updated_at") or "")[:64],
                        "source_count": len(source_ids) if isinstance(source_ids, list) else 0,
                        "attachment_count": (
                            len(attachments) if isinstance(attachments, list) else 0
                        ),
                    }
                )
            return len(rows), drafts
        except DraftUploadError:
            raise
        except (DraftPackageError, OSError, UnicodeError, ValueError):
            raise _invalid_package() from None

    def _remove_receipt(self, path: Path) -> None:
        try:
            self._ensure_roots()
            candidate = self.receipts_root / path.name
            if not _RECEIPT_NAME.fullmatch(candidate.name):
                return
            if candidate.is_symlink():
                candidate.unlink(missing_ok=True)
            elif candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
        except (DraftUploadError, OSError):
            return

    def _drop_locked(self, receipt_id: str) -> None:
        record = self._records.pop(receipt_id, None)
        if record is None:
            return
        self._receipt_ids_by_path.pop(record.path, None)
        self._remove_receipt(record.path)

    def _cleanup_locked(self) -> int:
        now = float(self.clock())
        expired = [
            record.receipt_id
            for record in self._records.values()
            if now - record.touched_at > self.receipt_ttl_seconds
        ]
        for receipt_id in expired:
            self._drop_locked(receipt_id)
        excess = max(0, len(self._records) - self.max_receipts)
        if excess:
            oldest = sorted(
                self._records.values(),
                key=lambda record: (record.touched_at, record.sequence),
            )[:excess]
            for record in oldest:
                self._drop_locked(record.receipt_id)
        return len(expired) + excess

    def cleanup(self) -> int:
        """Remove expired/excess previews and their private receipt directories."""

        with self._lock:
            self._ensure_roots()
            return self._cleanup_locked()

    def preview(self, filename: str, payload: UploadPayload) -> dict[str, object]:
        return self.preview_uploads([(filename, payload)])

    def preview_uploads(
        self,
        uploads: Iterable[tuple[str, UploadPayload]],
    ) -> dict[str, object]:
        selected: tuple[str, UploadPayload] | None = None
        try:
            for index, upload in enumerate(uploads):
                if index >= MAX_DRAFT_UPLOAD_FILES:
                    raise DraftUploadError(
                        400,
                        "invalid_upload_count",
                        "Upload exactly one draft package.",
                    )
                if not isinstance(upload, tuple) or len(upload) != 2:
                    raise DraftUploadError(
                        400,
                        "invalid_draft_upload",
                        "The draft upload is malformed.",
                    )
                selected = upload
        except DraftUploadError:
            raise
        except Exception:
            raise DraftUploadError(
                400,
                "invalid_draft_upload",
                "The draft upload is malformed.",
            ) from None
        if selected is None:
            raise DraftUploadError(
                400,
                "invalid_upload_count",
                "Upload exactly one draft package.",
            )
        filename, payload = selected
        self._validate_filename(filename)

        with self._lock:
            self._ensure_roots()
            self._cleanup_locked()
            stage = self._new_stage()
            try:
                package = self._write_upload(stage, payload)
                self._validate_archive_budget(package)
                try:
                    received = self._receiver(package, self.receipts_root)
                except (DraftPackageError, OSError, UnicodeError, ValueError):
                    raise _invalid_package() from None
                except Exception:
                    raise _invalid_package() from None
                if not isinstance(received, dict):
                    raise _invalid_package()
                path = self._safe_receipt_path(received.get("receipt_path"))
                draft_count, drafts = self._safe_drafts(path)
                if received.get("draft_count") != draft_count:
                    raise _invalid_package()
                existing = received.get("existing")
                if type(existing) is not bool:
                    raise _invalid_package()

                now = float(self.clock())
                current_id = self._receipt_ids_by_path.get(path)
                current = self._records.get(current_id or "")
                if current is None:
                    receipt_id = secrets.token_urlsafe(32)
                    while receipt_id in self._records:
                        receipt_id = secrets.token_urlsafe(32)
                    self._sequence += 1
                    current = _PreviewRecord(
                        receipt_id=receipt_id,
                        path=path,
                        draft_count=draft_count,
                        drafts=drafts,
                        touched_at=now,
                        sequence=self._sequence,
                    )
                    self._records[receipt_id] = current
                    self._receipt_ids_by_path[path] = receipt_id
                else:
                    current.touched_at = now
                    current.draft_count = draft_count
                    current.drafts = drafts
                self._cleanup_locked()
                return {
                    "receipt_id": current.receipt_id,
                    "draft_count": current.draft_count,
                    "existing": existing,
                    "drafts": [dict(row) for row in current.drafts],
                }
            finally:
                self._remove_stage(stage)

    def _current_record(self, receipt_id: object) -> _PreviewRecord:
        if not isinstance(receipt_id, str) or not _RECEIPT_ID.fullmatch(receipt_id):
            raise DraftUploadError(
                410,
                "preview_unavailable",
                "This draft preview is no longer available.",
            )
        record = self._records.get(receipt_id)
        if record is None:
            raise DraftUploadError(
                410,
                "preview_unavailable",
                "This draft preview is no longer available.",
            )
        self._safe_receipt_path(str(record.path))
        return record

    def accept(
        self,
        receipt_id: str,
        vault: Path,
        *,
        confirm: bool,
    ) -> dict[str, object]:
        if confirm is not True:
            raise DraftUploadError(
                409,
                "confirmation_required",
                "Confirm the current draft preview before accepting it.",
            )
        with self._lock:
            self._ensure_roots()
            self._cleanup_locked()
            record = self._current_record(receipt_id)
            vault_path = Path(vault).expanduser()
            try:
                if vault_path.is_symlink():
                    raise DraftUploadError(
                        409,
                        "draft_accept_failed",
                        "The draft package could not be accepted safely.",
                    )
                result = self._acceptor(record.path, vault_path)
            except DraftUploadError:
                raise
            except (DraftPackageError, OSError, UnicodeError, ValueError):
                raise DraftUploadError(
                    409,
                    "draft_accept_failed",
                    "The draft package could not be accepted safely.",
                ) from None
            except Exception:
                raise DraftUploadError(
                    409,
                    "draft_accept_failed",
                    "The draft package could not be accepted safely.",
                ) from None
            if not isinstance(result, dict):
                raise DraftUploadError(
                    409,
                    "draft_accept_failed",
                    "The draft package could not be accepted safely.",
                )
            counts: dict[str, int] = {}
            for field in ("created", "unchanged", "conflicts", "draft_count"):
                value = result.get(field)
                if type(value) is not int or value < 0:
                    raise DraftUploadError(
                        409,
                        "draft_accept_failed",
                        "The draft package could not be accepted safely.",
                    )
                counts[field] = value
            if counts["draft_count"] != record.draft_count:
                raise DraftUploadError(
                    409,
                    "draft_accept_failed",
                    "The draft package could not be accepted safely.",
                )
            record.touched_at = float(self.clock())
            return {
                "receipt_id": record.receipt_id,
                "accepted": True,
                **counts,
            }
