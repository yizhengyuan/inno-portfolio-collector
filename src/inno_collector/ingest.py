from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit

from .identity import article_key, canonical_url
from .models import IngestResult, NormalizedArticle, ProjectAccount, RejectedArticle


_MIN_BODY_CHARACTERS = 80
_PROMPT_COVERAGE_THRESHOLD = 0.25
_LOGIN_PROMPTS = (
    "扫码登录",
    "请登录",
    "验证码",
    "登录后继续",
    "安全验证",
)
_DOWNLOAD_ERROR_TEMPLATES = (
    "下载失败",
    "获取文章失败",
    "此内容发送失败无法查看",
    "此内容因违规无法查看",
    "该内容已被发布者删除",
    "此内容已被删除",
    "当前内容暂时无法查看",
    "已停止访问该网页",
    "该内容无法查看",
)


def yaml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def ingest_account_output(project: ProjectAccount, root: Path) -> IngestResult:
    output_root = root.resolve()
    valid: list[NormalizedArticle] = []
    rejected: list[RejectedArticle] = []
    seen_keys: set[str] = set()

    with (output_root / "index.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        for row in csv.DictReader(stream):
            title = _safe_string(row.get("title")).strip()
            raw_url = _safe_string(row.get("source_url")).strip()
            rejected_url = _safe_rejected_url(raw_url)

            if _safe_string(row.get("status")).strip().casefold() != "success":
                rejected.append(RejectedArticle(title, rejected_url, "download_failed"))
                continue

            published = _published_date(row.get("publish_time"))
            if not title or published is None:
                rejected.append(RejectedArticle(title, rejected_url, "invalid_metadata"))
                continue

            try:
                source_url = canonical_url(raw_url)
                key = article_key(source_url)
            except ValueError:
                rejected.append(RejectedArticle(title, rejected_url, "invalid_url"))
                continue

            if key in seen_keys:
                rejected.append(RejectedArticle(title, source_url, "duplicate"))
                continue

            source_markdown, path_reason = _source_markdown(
                output_root, row.get("markdown_path")
            )
            if path_reason is not None:
                rejected.append(RejectedArticle(title, source_url, path_reason))
                continue

            assert source_markdown is not None
            try:
                raw_body = source_markdown.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                rejected.append(RejectedArticle(title, source_url, "invalid_body"))
                continue
            except OSError:
                rejected.append(RejectedArticle(title, source_url, "missing_file"))
                continue

            body = _normalize_body(raw_body)
            if _invalid_body(body):
                rejected.append(RejectedArticle(title, source_url, "invalid_body"))
                continue

            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            source_image_dir = _source_image_dir(
                output_root, row.get("image_dir")
            )
            valid.append(
                NormalizedArticle(
                    key=key,
                    project=project.project,
                    account=project.account,
                    title=title,
                    published=published,
                    source_url=source_url,
                    collected_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    content_hash=f"sha256:{digest}",
                    body=body,
                    source_markdown=source_markdown,
                    source_image_dir=source_image_dir,
                )
            )
            seen_keys.add(key)

    return IngestResult(valid=tuple(valid), rejected=tuple(rejected))


def _safe_string(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _safe_rejected_url(value: str) -> str:
    try:
        return canonical_url(value)
    except ValueError:
        pass

    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or hostname is None:
        return ""

    safe_hostname = _safe_hostname(hostname)
    if safe_hostname is None or any(
        ord(character) < 32 or ord(character) == 127 for character in parsed.path
    ):
        return ""
    if port is not None and port < 1:
        return ""

    default_port = 80 if scheme == "http" else 443
    netloc = safe_hostname
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path, "", ""))


def _safe_hostname(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            hostname = value.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError:
            return None
        labels = hostname.split(".")
        if not hostname or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in labels
        ):
            return None
        return hostname.casefold()
    if address.version == 6:
        return f"[{address.compressed}]"
    return address.compressed


def _published_date(value: object) -> str | None:
    text = _safe_string(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parsed = date.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed.isoformat()


def _source_markdown(root: Path, value: object) -> tuple[Path | None, str | None]:
    text = _safe_string(value).strip()
    relative = Path(text)
    if relative.is_absolute() or PureWindowsPath(text).is_absolute():
        return None, "invalid_path"

    try:
        resolved = (root / relative).resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, "invalid_path"

    try:
        if not resolved.is_file():
            return None, "missing_file"
    except OSError:
        return None, "missing_file"
    return resolved, None


def _source_image_dir(root: Path, value: object) -> Path | None:
    text = _safe_string(value).strip()
    if not text:
        return None
    relative = Path(text)
    if relative.is_absolute() or PureWindowsPath(text).is_absolute():
        return None

    try:
        resolved = (root / relative).resolve()
        resolved.relative_to(root)
        if not resolved.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _normalize_body(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def _invalid_body(body: str) -> bool:
    compact_body = "".join(
        character for character in body if not character.isspace()
    )
    character_count = len(compact_body)
    if character_count < _MIN_BODY_CHARACTERS:
        return True
    if any(template in body for template in _DOWNLOAD_ERROR_TEMPLATES):
        return True
    prompt_characters = sum(
        compact_body.count(prompt) * len(prompt) for prompt in _LOGIN_PROMPTS
    )
    return prompt_characters / character_count >= _PROMPT_COVERAGE_THRESHOLD
