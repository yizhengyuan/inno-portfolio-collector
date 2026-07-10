from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .ingest import yaml_string
from .models import NormalizedArticle, ProjectRunResult, VaultApplyResult
from .state import ManifestStore


_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\[\]]')
_SHA256_KEY = re.compile(r"^sha256:([0-9a-fA-F]{8,})$")
_PUBLISHED_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IMAGE_LINK = re.compile(r"(!\[[^\]\n]*\]\()([^\)\n]+)(\))")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _safe_filename(value: str, fallback: str, limit: int = 96) -> str:
    normalized = unicodedata.normalize("NFC", str(value))
    normalized = "".join(
        "-" if unicodedata.category(character) == "Cc" else character
        for character in normalized
    )
    cleaned = _UNSAFE_FILENAME.sub("-", normalized).strip().rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    pieces: list[str] = []
    used_bytes = 0
    for character in cleaned:
        character_bytes = len(character.encode("utf-8"))
        if used_bytes + character_bytes > limit:
            break
        pieces.append(character)
        used_bytes += character_bytes
    return "".join(pieces).rstrip(". ") or fallback


def _key_suffix(key: str) -> str:
    match = _SHA256_KEY.fullmatch(key)
    if match is not None:
        return match.group(1)[:8].lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _safe_relative_path(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _article_relative_path(value: object) -> PurePosixPath | None:
    path = _safe_relative_path(value)
    if (
        path is None
        or len(path.parts) < 3
        or path.parts[0] != "03-文章"
        or path.suffix.casefold() != ".md"
    ):
        return None
    return path


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        with os.fdopen(descriptor, "rb") as input_handle:
            descriptor = -1
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=destination.name + ".",
                suffix=".tmp",
                delete=False,
            ) as output_handle:
                temporary = Path(output_handle.name)
                shutil.copyfileobj(input_handle, output_handle)
                output_handle.flush()
                os.fsync(output_handle.fileno())
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _render_article(
    article: NormalizedArticle,
    read_status: str,
    body: str,
) -> bytes:
    fields = (
        ("project", article.project),
        ("account", article.account),
        ("title", article.title),
        ("published", article.published),
        ("source_url", article.source_url),
        ("collected_at", article.collected_at),
        ("content_hash", article.content_hash),
        ("read_status", read_status),
    )
    frontmatter = "\n".join(f"{name}: {yaml_string(value)}" for name, value in fields)
    body = body.lstrip("\n")
    return f"---\n{frontmatter}\n---\n\n{body}".encode("utf-8")


def _read_status(path: Path, fallback: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return fallback
    if not lines or lines[0].strip() != "---":
        return fallback
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.startswith("read_status:"):
            continue
        try:
            value = json.loads(line.partition(":")[2].strip())
        except (json.JSONDecodeError, TypeError):
            return fallback
        return value if isinstance(value, str) else fallback
    return fallback


def _plain_cell(value: object) -> str:
    text = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in str(value)
    )
    text = " ".join(text.split())
    return html.escape(text, quote=False).replace("|", "\\|")


def _table(project_results: list[ProjectRunResult]) -> str:
    rows = [
        "| project | account | discovered | downloaded | skipped | failed | "
        "status | error |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for result in project_results:
        rows.append(
            "| "
            + " | ".join(
                (
                    _plain_cell(result.project),
                    _plain_cell(result.account),
                    str(result.discovered),
                    str(result.downloaded),
                    str(result.skipped),
                    str(result.failed),
                    _plain_cell(result.status),
                    _plain_cell(result.error),
                )
            )
            + " |"
        )
    return "\n".join(rows)


class VaultWriter:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, relative: PurePosixPath | str) -> Path:
        raw_path = relative if isinstance(relative, str) else relative.as_posix()
        posix = _safe_relative_path(raw_path)
        if posix is None:
            raise ValueError("unsafe vault path")
        candidate = self.root.joinpath(*posix.parts)
        try:
            candidate.resolve(strict=False).relative_to(self.root)
        except (OSError, RuntimeError, ValueError):
            raise ValueError("unsafe vault path") from None
        return candidate

    def _new_article_path(self, article: NormalizedArticle) -> PurePosixPath:
        return self._new_record_path(
            article.key,
            article.project,
            article.title,
            article.published,
        )

    def _new_record_path(
        self,
        key: str,
        project_value: object,
        title_value: object,
        published_value: object,
    ) -> PurePosixPath:
        project = _safe_filename(str(project_value), "未命名项目", 80)
        title = _safe_filename(str(title_value), "未命名文章", 96)
        published_text = str(published_value)
        published = (
            published_text
            if _PUBLISHED_DATE.fullmatch(published_text)
            else "0000-00-00"
        )
        filename = f"{published}-{title}-{_key_suffix(key)}.md"
        return PurePosixPath("03-文章", project, filename)

    def _attachment_root(self, article: NormalizedArticle) -> PurePosixPath:
        project = _safe_filename(article.project, "未命名项目", 80)
        title = _safe_filename(article.title, "未命名文章", 80)
        return PurePosixPath(
            "04-附件", project, f"{title}-{_key_suffix(article.key)}"
        )

    def _copy_attachments(
        self, article: NormalizedArticle
    ) -> tuple[list[str], dict[str, PurePosixPath]]:
        source = article.source_image_dir
        if source is None:
            return [], {}
        source_path = Path(source)
        try:
            if source_path.is_symlink() or not source_path.is_dir():
                return [], {}
            source_root = source_path.resolve(strict=True)
        except (OSError, RuntimeError):
            return [], {}

        attachment_root = self._attachment_root(article)
        copied: list[str] = []
        mapping: dict[str, PurePosixPath] = {}
        for directory, directory_names, filenames in os.walk(
            source_root, topdown=True, followlinks=False
        ):
            current = Path(directory)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not name.startswith(".") and not (current / name).is_symlink()
            )
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                candidate = current / filename
                if candidate.suffix.casefold() not in _IMAGE_EXTENSIONS:
                    continue
                try:
                    details = candidate.lstat()
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(source_root)
                except (OSError, RuntimeError, ValueError):
                    continue
                if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                    continue
                relative_source = resolved.relative_to(source_root)
                relative_posix = PurePosixPath(*relative_source.parts)
                destination_relative = attachment_root / relative_posix
                destination = self._path(destination_relative)
                _atomic_copy(resolved, destination)
                destination_text = destination_relative.as_posix()
                copied.append(destination_text)
                mapping[relative_posix.as_posix()] = destination_relative
        return sorted(copied), mapping

    def _rewrite_links(
        self,
        article: NormalizedArticle,
        mapping: dict[str, PurePosixPath],
    ) -> str:
        source = article.source_image_dir
        if source is None or not mapping:
            return article.body
        source_name = Path(source).name

        def replace_link(match: re.Match[str]) -> str:
            raw_target = match.group(2)
            if raw_target.casefold().startswith(("http://", "https://")):
                return match.group(0)
            decoded = unquote(raw_target)
            target = PurePosixPath(decoded)
            if (
                len(target.parts) < 4
                or target.parts[:3] != ("..", "images", source_name)
                or any(part in {"", ".", ".."} for part in target.parts[3:])
            ):
                return match.group(0)
            source_relative = PurePosixPath(*target.parts[3:]).as_posix()
            copied = mapping.get(source_relative)
            if copied is None:
                return match.group(0)
            rewritten = (PurePosixPath("..", "..") / copied).as_posix()
            rewritten = rewritten.replace(" ", "%20")
            return f"{match.group(1)}{rewritten}{match.group(3)}"

        return _IMAGE_LINK.sub(replace_link, article.body)

    def _safe_attachments(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        safe: list[str] = []
        for attachment in value:
            relative = _safe_relative_path(attachment)
            if (
                relative is None
                or len(relative.parts) < 3
                or relative.parts[0] != "04-附件"
            ):
                continue
            try:
                path = self._path(relative)
                if path.is_symlink() or not path.is_file():
                    continue
            except (OSError, ValueError):
                continue
            safe.append(relative.as_posix())
        return sorted(set(safe))

    def _sanitize_manifest(self, store: ManifestStore) -> None:
        cleaned_records: dict[str, dict[str, object]] = {}
        for key, record in store.data["articles"].items():
            path = _article_relative_path(record.get("path"))
            if path is None:
                path = self._new_record_path(
                    key,
                    record.get("project", ""),
                    record.get("title", ""),
                    record.get("published", ""),
                )

            def text_field(name: str) -> str:
                value = record.get(name, "")
                return value if isinstance(value, str) else str(value)

            read_status = record.get("read_status", "unread")
            if not isinstance(read_status, str):
                read_status = "unread"
            cleaned_records[key] = {
                "key": key,
                "project": text_field("project"),
                "account": text_field("account"),
                "title": text_field("title"),
                "published": text_field("published"),
                "source_url": text_field("source_url"),
                "collected_at": text_field("collected_at"),
                "content_hash": text_field("content_hash"),
                "read_status": read_status,
                "path": path.as_posix(),
                "attachments": self._safe_attachments(record.get("attachments")),
            }
        store.data["articles"] = cleaned_records

    def _write_indexes(
        self,
        store: ManifestStore,
        project_results: list[ProjectRunResult],
    ) -> None:
        records = store.data["articles"]
        projects = {
            result.project
            for result in project_results
            if isinstance(result.project, str)
        }
        projects.update(
            record.get("project")
            for record in records.values()
            if isinstance(record.get("project"), str)
        )
        project_names = sorted(projects, key=lambda value: (value.casefold(), value))

        for project in project_names:
            articles = [
                (key, record)
                for key, record in records.items()
                if record.get("project") == project
                and _article_relative_path(record.get("path")) is not None
            ]
            articles.sort(
                key=lambda item: (
                    str(item[1].get("published", "")),
                    str(item[1].get("title", "")),
                    item[0],
                ),
                reverse=True,
            )
            lines = [f"# {_plain_cell(project)}", ""]
            if not articles:
                lines.append("暂无文章。")
            for _, record in articles:
                relative = _article_relative_path(record["path"])
                assert relative is not None
                target = PurePosixPath("..") / relative
                title = str(record.get("title", "未命名文章")).replace("|", "-")
                published = _plain_cell(record.get("published", ""))
                lines.append(f"- {published} [[{target.as_posix()}|{title}]]")
            page_name = _safe_filename(project, "未命名项目", 80) + ".md"
            _atomic_write(
                self._path(PurePosixPath("02-项目", page_name)),
                ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
            )

        home_lines = [
            "# 英诺项目文章库",
            "",
            "[[01-采集状态|采集状态]]",
            "",
            f"总文章数：{len(records)}",
            "",
            "## 项目",
            "",
        ]
        home_lines.extend(
            f"- [[02-项目/{_safe_filename(project, '未命名项目', 80)}|"
            f"{_plain_cell(project)}]]"
            for project in project_names
        )
        _atomic_write(
            self._path("00-首页.md"),
            ("\n".join(home_lines).rstrip() + "\n").encode("utf-8"),
        )

        status = "# 采集状态\n\n" + _table(project_results) + "\n"
        _atomic_write(self._path("01-采集状态.md"), status.encode("utf-8"))

        failed_projects = sum(
            result.failed > 0
            or result.status.strip().casefold() not in {"success", "ok", "completed"}
            for result in project_results
        )
        report = (
            "# 本次采集报告\n\n"
            f"- 项目数：{len(project_results)}\n"
            f"- 失败项目数：{failed_projects}\n"
            f"- 文章总数：{len(records)}\n\n"
            "## 项目统计\n\n"
            + _table(project_results)
            + "\n"
        )
        _atomic_write(
            self._path("90-系统/collection-report.md"), report.encode("utf-8")
        )
        readme = (
            "# 使用说明\n\n"
            "请在 Obsidian 中将本目录作为仓库打开。\n\n"
            "文章 frontmatter 中的 `read_status` 默认为 `unread`，"
            "可人工修改；"
            "后续内容更新会优先保留该值。\n\n"
            "`90-系统` 保存 manifest、采集报告和本说明，"
            "请勿随意删除。\n"
        )
        _atomic_write(self._path("90-系统/README.md"), readme.encode("utf-8"))

    def apply(
        self,
        articles: list[NormalizedArticle],
        project_results: list[ProjectRunResult],
    ) -> VaultApplyResult:
        store = ManifestStore(self._path("90-系统/manifest.json"))
        created = updated = unchanged = 0
        seen: set[str] = set()

        for article in articles:
            if article.key in seen:
                continue
            seen.add(article.key)
            existing = store.get(article.key)
            relative = (
                None
                if existing is None
                else _article_relative_path(existing.get("path"))
            )
            if relative is None:
                relative = self._new_article_path(article)
            destination = self._path(relative)
            read_status = "unread"
            if existing is not None and isinstance(existing.get("read_status"), str):
                read_status = existing["read_status"]
            if destination.is_file():
                read_status = _read_status(destination, read_status)

            attachments, attachment_mapping = self._copy_attachments(article)
            if article.source_image_dir is None and existing is not None:
                attachments = self._safe_attachments(existing.get("attachments"))
            body = self._rewrite_links(article, attachment_mapping)

            same_hash = (
                existing is not None
                and existing.get("content_hash") == article.content_hash
                and destination.is_file()
            )
            if same_hash:
                unchanged += 1
            else:
                _atomic_write(
                    destination,
                    _render_article(article, read_status, body),
                )
                if existing is None:
                    created += 1
                else:
                    updated += 1

            store.upsert(
                article.key,
                {
                    "key": article.key,
                    "project": article.project,
                    "account": article.account,
                    "title": article.title,
                    "published": article.published,
                    "source_url": article.source_url,
                    "collected_at": article.collected_at,
                    "content_hash": article.content_hash,
                    "read_status": read_status,
                    "path": relative.as_posix(),
                    "attachments": attachments,
                },
            )

        self._sanitize_manifest(store)
        store.save()
        self._write_indexes(store, project_results)
        return VaultApplyResult(created, updated, unchanged)
