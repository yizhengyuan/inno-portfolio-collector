from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebResponse:
    status: int
    body: bytes
    content_type: str
    inject_session_token: bool = False
