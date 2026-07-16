from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class WebResponse:
    status: int
    body: bytes
    content_type: str
    inject_session_token: bool = False


@dataclass(frozen=True, slots=True)
class FileResponse:
    path: Path
    filename: str
    content_type: str
    size: int
    sha256: str
    on_complete: Callable[[bool], None] | None = None
