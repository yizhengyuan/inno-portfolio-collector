from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            data: object = {"version": 1, "articles": {}}
        else:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)

        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("articles"), dict)
        ):
            raise ValueError("unsupported manifest format")

        self._data = data
        return self._data

    def get(self, key: str) -> dict[str, Any] | None:
        article = self._data["articles"].get(key)
        return None if article is None else dict(article)

    def upsert(self, key: str, article: dict[str, Any]) -> None:
        self._data["articles"][key] = dict(article)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                self._data,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
