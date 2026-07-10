from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


STABLE_QUERY_KEYS = {"__biz", "mid", "idx", "sn"}


def canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
    except (AttributeError, TypeError, ValueError):
        raise ValueError("unsupported article URL") from None

    if parsed.scheme not in {"http", "https"} or hostname != "mp.weixin.qq.com":
        raise ValueError("unsupported article URL")

    path = parsed.path.rstrip("/") or "/s"
    query = ""
    if path == "/s":
        stable_items = sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key in STABLE_QUERY_KEYS
        )
        query = urlencode(stable_items)

    return urlunsplit(("https", "mp.weixin.qq.com", path, query, ""))


def article_key(value: str) -> str:
    canonical = canonical_url(value)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _published_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def select_since(
    rows: Iterable[Mapping[str, Any]], since: str
) -> list[Mapping[str, Any]]:
    cutoff = date.fromisoformat(since)
    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        published = _published_date(row.get("publish_time"))
        url = row.get("url")
        if published is None or published < cutoff or not url:
            continue

        key = article_key(url)
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)

    selected.sort(
        key=lambda row: (
            str(row.get("publish_time") or ""),
            int(row.get("id") or 0),
        ),
        reverse=True,
    )
    return selected
