from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def _validate_manifest(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("unsupported manifest format") from None
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("unsupported manifest format") from None

    articles = data.get("articles")
    if not isinstance(articles, dict):
        raise ValueError("unsupported manifest format") from None
    if any(
        not isinstance(key, str) or not isinstance(record, dict)
        for key, record in articles.items()
    ):
        raise ValueError("unsupported manifest format") from None
    return data


class ManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data = self.load()

    def _validate_data(self, data: object) -> dict[str, Any]:
        return _validate_manifest(data)

    def _read_data(self, path: Path) -> dict[str, Any]:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._validate_data(None)
        return self._validate_data(data)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            data = {"version": 1, "articles": {}}
        else:
            data = self._read_data(self.path)
        self.data = data
        self._base_articles = copy.deepcopy(data["articles"])
        return data

    def get(self, key: str) -> dict[str, Any] | None:
        article = self.data["articles"].get(key)
        return None if article is None else copy.deepcopy(article)

    def upsert(self, key: str, article: dict[str, Any]) -> None:
        self.data["articles"][key] = copy.deepcopy(article)

    def save(self) -> None:
        self._validate_data(self.data)
        current_articles = self.data["articles"]
        changed_keys = {
            key
            for key in self._base_articles.keys() | current_articles.keys()
            if key not in self._base_articles
            or key not in current_articles
            or self._base_articles[key] != current_articles[key]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        temporary_path: Path | None = None

        with lock_path.open("a") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    disk_data = self._read_data(self.path)
                else:
                    disk_data = {"version": 1, "articles": {}}

                merged = {
                    "version": 1,
                    "articles": copy.deepcopy(disk_data["articles"]),
                }
                for key in changed_keys:
                    if key in current_articles:
                        merged["articles"][key] = copy.deepcopy(current_articles[key])
                    else:
                        merged["articles"].pop(key, None)
                self._validate_data(merged)

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=self.path.name + ".",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    json.dump(
                        merged,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                os.replace(temporary_path, self.path)
                self.data = copy.deepcopy(merged)
                self._base_articles = copy.deepcopy(merged["articles"])
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_catalog_state(data: object) -> dict[str, Any]:
    try:
        manifest = _validate_manifest(data)
    except ValueError:
        raise ValueError("unsupported catalog state format") from None
    for record in manifest["articles"].values():
        if not isinstance(record.get("fingerprint"), str) or _FINGERPRINT.fullmatch(
            record["fingerprint"]
        ) is None:
            raise ValueError("unsupported catalog state format") from None
        if set(record) == {"fingerprint"}:
            continue
        if (
            set(record) != {"fingerprint", "content_hash", "verified_at"}
            or not isinstance(record.get("content_hash"), str)
            or _FINGERPRINT.fullmatch(record["content_hash"]) is None
            or not isinstance(record.get("verified_at"), str)
        ):
            raise ValueError("unsupported catalog state format") from None
        try:
            verified_at = datetime.fromisoformat(
                record["verified_at"].replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError("unsupported catalog state format") from None
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise ValueError("unsupported catalog state format") from None
    return manifest


class CatalogStateStore(ManifestStore):
    def _validate_data(self, data: object) -> dict[str, Any]:
        return _validate_catalog_state(data)

    def get(self, key: str) -> str | None:
        record = self.get_record(key)
        if record is None:
            return None
        return str(record["fingerprint"])

    def get_record(self, key: str) -> dict[str, Any] | None:
        return super().get(key)

    def mark_success(
        self,
        key: str,
        fingerprint: str,
        *,
        content_hash: str | None = None,
        verified_at: str | None = None,
    ) -> None:
        record = {"fingerprint": fingerprint}
        if content_hash is not None or verified_at is not None:
            record.update(
                {
                    "content_hash": content_hash,
                    "verified_at": verified_at,
                }
            )
        _validate_catalog_state(
            {"version": 1, "articles": {key: record}}
        )
        super().upsert(key, record)
