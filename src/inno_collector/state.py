from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
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


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("unsupported manifest format") from None
    return _validate_manifest(data)


class ManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data = self.load()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            data = {"version": 1, "articles": {}}
        else:
            data = _read_manifest(self.path)
        self.data = data
        return data

    def get(self, key: str) -> dict[str, Any] | None:
        article = self.data["articles"].get(key)
        return None if article is None else copy.deepcopy(article)

    def upsert(self, key: str, article: dict[str, Any]) -> None:
        self.data["articles"][key] = copy.deepcopy(article)

    def save(self) -> None:
        _validate_manifest(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        temporary_path: Path | None = None

        with lock_path.open("a") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    disk_data = _read_manifest(self.path)
                else:
                    disk_data = {"version": 1, "articles": {}}

                merged = {
                    "version": 1,
                    "articles": copy.deepcopy(disk_data["articles"]),
                }
                merged["articles"].update(copy.deepcopy(self.data["articles"]))
                _validate_manifest(merged)

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
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
