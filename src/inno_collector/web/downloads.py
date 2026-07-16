from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_MIME_TYPE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$"
)
_HASH_CHUNK_BYTES = 1 << 20


class DownloadRegistryError(RuntimeError):
    """Base class for stable, user-safe download registry errors."""


class DownloadGoneError(DownloadRegistryError):
    pass


class DownloadRegistrationError(DownloadRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    expires_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "sha256": self.sha256,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class DownloadClaim:
    id: str
    path: Path
    filename: str
    content_type: str
    size: int
    sha256: str


@dataclass(slots=True)
class _Entry:
    record: DownloadRecord
    path: Path
    device: int
    inode: int
    claimed: bool = False


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(first: Path, second: Path) -> bool:
    return _within(first, second) or _within(second, first)


def _canonical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path))).resolve(strict=False)


def _safe_filename(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise DownloadRegistrationError("download metadata is invalid")
    if value != Path(value).name or "/" in value or "\\" in value:
        raise DownloadRegistrationError("download metadata is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DownloadRegistrationError("download metadata is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise DownloadRegistrationError("download metadata is invalid") from error
    if len(encoded) > 255:
        raise DownloadRegistrationError("download metadata is invalid")
    return value


def _safe_content_type(value: object) -> str:
    if not isinstance(value, str) or not _MIME_TYPE.fullmatch(value):
        raise DownloadRegistrationError("download metadata is invalid")
    return value.lower()


class DownloadRegistry:
    """Thread-safe registry for short-lived, app-created delivery files."""

    def __init__(
        self,
        delivery_root: Path,
        *,
        vault_root: Path,
        exporter_runtime_root: Path,
        ttl_seconds: float = 15 * 60,
        max_entries: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")

        requested_root = Path(os.path.abspath(os.fspath(delivery_root)))
        forbidden = (
            _canonical(Path(vault_root)),
            _canonical(Path(exporter_runtime_root)),
        )
        tentative_root = requested_root.resolve(strict=False)
        if any(_overlaps(tentative_root, item) for item in forbidden):
            raise ValueError("delivery root overlaps protected storage")

        try:
            requested_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_stat = requested_root.lstat()
        except OSError as error:
            raise ValueError("delivery root is unavailable") from error
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("delivery root is unavailable")

        resolved_root = requested_root.resolve(strict=True)
        if any(_overlaps(resolved_root, item) for item in forbidden):
            raise ValueError("delivery root overlaps protected storage")

        self._root = requested_root
        self._resolved_root = resolved_root
        self._forbidden_roots = forbidden
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max_entries
        self.clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()

    def _eligible_path(self, value: Path) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        candidate = Path(os.path.abspath(os.fspath(candidate)))
        try:
            relative = candidate.relative_to(self._root)
        except ValueError as error:
            raise DownloadRegistrationError(
                "download file is not eligible"
            ) from error

        current = self._root
        try:
            for index, part in enumerate(relative.parts):
                current = current / part
                current_stat = current.lstat()
                if stat.S_ISLNK(current_stat.st_mode):
                    raise DownloadRegistrationError(
                        "download file is not eligible"
                    )
                if index < len(relative.parts) - 1 and not stat.S_ISDIR(
                    current_stat.st_mode
                ):
                    raise DownloadRegistrationError(
                        "download file is not eligible"
                    )
            target_stat = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except DownloadRegistrationError:
            raise
        except OSError as error:
            raise DownloadRegistrationError(
                "download file is not eligible"
            ) from error

        if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:
            raise DownloadRegistrationError("download file is not eligible")
        if not _within(resolved, self._resolved_root):
            raise DownloadRegistrationError("download file is not eligible")
        if any(_within(resolved, item) for item in self._forbidden_roots):
            raise DownloadRegistrationError("download file is not eligible")
        return candidate

    @staticmethod
    def _read_file(
        path: Path,
        *,
        collect: bool,
    ) -> tuple[bytes | None, os.stat_result, str]:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise DownloadGoneError("download is unavailable") from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise DownloadGoneError("download is unavailable")
            chunks: list[bytes] | None = [] if collect else None
            digest = hashlib.sha256()
            bytes_read = 0
            while True:
                chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            after = os.fstat(descriptor)
            unchanged = (
                before.st_dev == after.st_dev
                and before.st_ino == after.st_ino
                and before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
                and before.st_ctime_ns == after.st_ctime_ns
                and bytes_read == after.st_size
            )
            if not unchanged:
                raise DownloadGoneError("download is unavailable")
            payload = b"".join(chunks) if chunks is not None else None
            return payload, after, digest.hexdigest()
        except OSError as error:
            raise DownloadGoneError("download is unavailable") from error
        finally:
            os.close(descriptor)

    @staticmethod
    def _matches(entry: _Entry, digest: str, file_stat: os.stat_result) -> bool:
        return (
            file_stat.st_dev == entry.device
            and file_stat.st_ino == entry.inode
            and file_stat.st_size == entry.record.size
            and digest == entry.record.sha256
        )

    def _delete_file_locked(self, entry: _Entry) -> None:
        try:
            file_stat = entry.path.lstat()
            if (
                stat.S_ISREG(file_stat.st_mode)
                and file_stat.st_dev == entry.device
                and file_stat.st_ino == entry.inode
            ):
                entry.path.unlink()
        except OSError:
            return

    def _drop_locked(self, download_id: str) -> bool:
        entry = self._entries.pop(download_id, None)
        if entry is None:
            return False
        self._delete_file_locked(entry)
        return True

    def _cleanup_locked(self) -> int:
        now = self.clock()
        expired = [
            download_id
            for download_id, entry in self._entries.items()
            if not entry.claimed and now >= entry.record.expires_at
        ]
        removed = 0
        for download_id in expired:
            removed += int(self._drop_locked(download_id))
        while len(self._entries) > self.max_entries:
            oldest = next(
                (
                    download_id
                    for download_id, entry in self._entries.items()
                    if not entry.claimed
                ),
                None,
            )
            if oldest is None:
                break
            removed += int(self._drop_locked(oldest))
        return removed

    def register(
        self,
        path: Path,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> DownloadRecord:
        with self._lock:
            self._cleanup_locked()
            eligible = self._eligible_path(Path(path))
            safe_name = _safe_filename(filename if filename is not None else eligible.name)
            guessed = mimetypes.guess_type(safe_name, strict=False)[0]
            safe_type = _safe_content_type(
                content_type or guessed or "application/octet-stream"
            )
            try:
                _, file_stat, digest = self._read_file(eligible, collect=False)
            except DownloadGoneError as error:
                raise DownloadRegistrationError(
                    "download file is not eligible"
                ) from error
            if any(
                entry.device == file_stat.st_dev and entry.inode == file_stat.st_ino
                for entry in self._entries.values()
            ):
                raise DownloadRegistrationError("download file is already registered")
            if len(self._entries) >= self.max_entries:
                oldest = next(
                    (
                        download_id
                        for download_id, entry in self._entries.items()
                        if not entry.claimed
                    ),
                    None,
                )
                if oldest is None:
                    raise DownloadRegistrationError("download registry is busy")
                self._drop_locked(oldest)

            download_id = secrets.token_urlsafe(24)
            while download_id in self._entries:
                download_id = secrets.token_urlsafe(24)
            record = DownloadRecord(
                id=download_id,
                filename=safe_name,
                content_type=safe_type,
                size=file_stat.st_size,
                sha256=digest,
                expires_at=self.clock() + self.ttl_seconds,
            )
            self._entries[download_id] = _Entry(
                record=record,
                path=eligible,
                device=file_stat.st_dev,
                inode=file_stat.st_ino,
            )
            self._cleanup_locked()
            return record

    def _entry_locked(self, download_id: str) -> _Entry:
        self._cleanup_locked()
        entry = self._entries.get(download_id)
        if entry is None:
            raise DownloadGoneError("download is unavailable")
        return entry

    def _validate_locked(
        self,
        download_id: str,
        *,
        collect: bool,
    ) -> tuple[_Entry, bytes | None]:
        entry = self._entry_locked(download_id)
        try:
            eligible = self._eligible_path(entry.path)
            if eligible != entry.path:
                raise DownloadGoneError("download is unavailable")
            payload, file_stat, digest = self._read_file(
                entry.path,
                collect=collect,
            )
        except (DownloadGoneError, DownloadRegistrationError):
            self._drop_locked(download_id)
            raise DownloadGoneError("download is unavailable") from None
        if not self._matches(entry, digest, file_stat):
            self._drop_locked(download_id)
            raise DownloadGoneError("download is unavailable")
        return entry, payload

    def get(self, download_id: str) -> DownloadRecord:
        with self._lock:
            return self._entry_locked(download_id).record

    def claim(self, download_id: str) -> DownloadClaim:
        with self._lock:
            existing = self._entry_locked(download_id)
            if existing.claimed:
                raise DownloadGoneError("download is unavailable")
            entry, _ = self._validate_locked(download_id, collect=False)
            entry.claimed = True
            record = entry.record
            return DownloadClaim(
                id=record.id,
                path=entry.path,
                filename=record.filename,
                content_type=record.content_type,
                size=record.size,
                sha256=record.sha256,
            )

    def read(self, download_id: str) -> bytes:
        with self._lock:
            _, payload = self._validate_locked(download_id, collect=True)
            if payload is None:
                raise DownloadGoneError("download is unavailable")
            return payload

    def complete(self, download_id: str, success: bool = True) -> None:
        if type(success) is not bool:
            raise TypeError("success must be a boolean")
        with self._lock:
            entry = self._entry_locked(download_id)
            if success:
                self._drop_locked(download_id)
                return
            entry.claimed = False
            if self.clock() >= entry.record.expires_at:
                self._drop_locked(download_id)
                return
            try:
                self._validate_locked(download_id, collect=False)
            except DownloadGoneError:
                return
            self._cleanup_locked()

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked()

    @property
    def count(self) -> int:
        with self._lock:
            self._cleanup_locked()
            return len(self._entries)
