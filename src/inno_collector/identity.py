from __future__ import annotations

import hashlib
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


STABLE_QUERY_KEYS = {"__biz", "mid", "idx", "sn"}


def canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError("unsupported article URL") from None

    if parsed.scheme not in {"http", "https"} or hostname != "mp.weixin.qq.com":
        raise ValueError("unsupported article URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("unsupported article URL")
    default_port = 80 if parsed.scheme == "http" else 443
    if port is not None and port != default_port:
        raise ValueError("unsupported article URL")

    path = parsed.path.rstrip("/") or "/s"
    if path == "/s":
        stable_items = sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key in STABLE_QUERY_KEYS
        )
        if (
            len(stable_items) != len(STABLE_QUERY_KEYS)
            or {key for key, _ in stable_items} != STABLE_QUERY_KEYS
            or any(not item for _, item in stable_items)
        ):
            raise ValueError("unsupported article URL")
        query = urlencode(stable_items)
    elif path.startswith("/s/") and path.count("/") == 2 and len(path) > 3:
        query = ""
    else:
        raise ValueError("unsupported article URL")

    return urlunsplit(("https", "mp.weixin.qq.com", path, query, ""))


def article_key(value: str) -> str:
    canonical = canonical_url(value)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _published_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _numeric_id(value: object) -> int:
    try:
        return int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0


def select_since_with_invalid_urls(
    rows: list[dict], since: str
) -> tuple[list[dict], int]:
    cutoff = date.fromisoformat(since)
    selected: list[dict] = []
    seen: set[str] = set()
    invalid_urls = 0

    for row in rows:
        published = _published_date(row.get("publish_time"))
        url = str(row.get("url") or "")
        if published is None or published < cutoff:
            continue

        try:
            key = article_key(url)
        except ValueError:
            invalid_urls += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)

    selected.sort(
        key=lambda row: (
            str(row.get("publish_time") or ""),
            _numeric_id(row.get("id")),
        ),
        reverse=True,
    )
    return selected, invalid_urls


def select_since(rows: list[dict], since: str) -> list[dict]:
    return select_since_with_invalid_urls(rows, since)[0]
