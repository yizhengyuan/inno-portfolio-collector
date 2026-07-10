from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .identity import article_key, canonical_url


class DeliveryValidationError(RuntimeError):
    def __init__(self, report: dict[str, object]) -> None:
        super().__init__("vault delivery validation failed")
        self.report = report


_REQUIRED_DIRECTORIES = ("02-项目", "03-文章", "04-附件", "90-系统")
_REQUIRED_FILES = (
    "00-首页.md",
    "01-采集状态.md",
    "90-系统/manifest.json",
    "90-系统/collection-report.md",
    "90-系统/README.md",
)
_RECORD_FIELDS = {
    "key", "project", "account", "title", "published", "source_url",
    "collected_at", "content_hash", "read_status", "path", "attachments",
}
_FRONTMATTER_FIELDS = (
    "project", "account", "title", "published", "source_url",
    "collected_at", "content_hash", "read_status",
)
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_IMAGE_LINK = re.compile(r"!\[[^\]\n]*\]\(([^)\n]+)\)")
_REPORT_NUMBER = re.compile(r"^- (项目数|失败项目数|文章总数)：(\d+)\s*$", re.MULTILINE)
_SECRET_VALUE = re.compile(
    r"(?i)(?<![\w-])(auth-key|pass_ticket|appmsg_token)\s*[:=]\s*"
    r"(?!\[REDACTED\](?:\W|$))([^\s,;&]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(?<![\w-])authorization\s*:\s*bearer\s+"
    r"(?!\[REDACTED\](?:\W|$))[^\s,;]+"
)
_COOKIE_HEADER = re.compile(
    r"(?im)^\s*cookie\s*:\s*(?!\[REDACTED\]\s*$)\S.*$"
)
_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:file://)?/(?:Users|Volumes|private|var|tmp|home|opt)(?:/[^\s<>\]\[)'\"]*)?|"
    r"(?<![\w])[a-z]:[\\/][^\s<>\]\[)'\"]+|"
    r"(?<!\\)\\\\[^\\\s]+\\[^\s<>\]\[)'\"]+"
)
_SUSPICIOUS_SUFFIXES = (
    ".tmp", ".temp", ".bak", ".backup", ".stage", ".lock", "~",
)
_SAFE_IGNORED_LOCKS = {".vault.lock", "90-系统/manifest.json.lock"}


def _collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _safe_relative(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _open_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise OSError("file changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _inventory(root: Path) -> tuple[list[PurePosixPath], list[PurePosixPath], list[str]]:
    files: list[PurePosixPath] = []
    directories: list[PurePosixPath] = []
    forbidden: list[str] = []
    try:
        root_details = root.lstat()
    except OSError:
        return files, directories, ["vault root is missing"]
    if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(root_details.st_mode):
        return files, directories, ["vault root is not a regular directory"]

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            forbidden.append(f"{relative.as_posix() or '.'}: unreadable directory")
            return
        for entry in entries:
            child = relative / entry.name
            child_text = child.as_posix()
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError:
                forbidden.append(f"{child_text}: unreadable")
                continue
            if "\\" in entry.name or entry.name in {".", ".."}:
                forbidden.append(f"{child_text}: unsafe name")
            elif stat.S_ISLNK(details.st_mode):
                forbidden.append(f"{child_text}: symbolic link")
            elif stat.S_ISDIR(details.st_mode):
                directories.append(child)
                visit(Path(entry.path), child)
            elif stat.S_ISREG(details.st_mode):
                files.append(child)
            else:
                forbidden.append(f"{child_text}: special file")

    visit(root, PurePosixPath())
    return files, directories, forbidden


def _is_forbidden(path: PurePosixPath) -> bool:
    text = path.as_posix()
    lowered = text.casefold()
    name = path.name.casefold()
    if text in _SAFE_IGNORED_LOCKS:
        return False
    if name == ".ds_store" or name.startswith("workspace") and name.endswith(".json"):
        return True
    if any(part.casefold() in {"runtime", "staging", ".staging"} for part in path.parts):
        return True
    if any(marker in name for marker in ("cookie", "token")) or name.endswith((".sqlite", ".sqlite3", ".db", ".db3")):
        return True
    if any(name.endswith(suffix) for suffix in _SUSPICIOUS_SUFFIXES):
        return True
    if name.startswith((".v.stage-", ".v.backup-")):
        return True
    return False


def _text(path: Path) -> str:
    return _open_regular(path).decode("utf-8")


def _frontmatter(path: Path) -> dict[str, object] | None:
    try:
        lines = _text(path).splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0] != "---":
        return None
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return None
    result: dict[str, object] = {}
    for line in lines[1:closing]:
        if ": " not in line:
            return None
        field, raw = line.split(": ", 1)
        if field in result:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        result[field] = value
    return result


def _resolve_local_target(
    source: PurePosixPath,
    raw_target: str,
    files: set[PurePosixPath],
    markdown_by_stem: dict[str, list[PurePosixPath]],
    *,
    markdown: bool,
) -> bool:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1:target.index(">")]
    elif not markdown:
        title = re.fullmatch(r'(.*?)(?:\s+["\'][^"\']*["\'])?', target)
        if title is not None:
            target = title.group(1)
    target = unquote(target)
    if not target:
        return True
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//"):
        return True
    if markdown:
        target = target.split("#", 1)[0].split("^", 1)[0]
        if not target:
            return True
    target = target.replace("\\", "/")
    if target.startswith("/"):
        parts: list[str] = []
        incoming = PurePosixPath(target.lstrip("/")).parts
    else:
        parts = list(source.parent.parts)
        incoming = PurePosixPath(target).parts
    for part in incoming:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
        else:
            parts.append(part)
    candidate = PurePosixPath(*parts)
    if markdown:
        choices = [candidate]
        if candidate.suffix.casefold() != ".md":
            choices.append(candidate.with_suffix(".md"))
        if any(choice in files for choice in choices):
            return True
        if "/" not in target:
            return len(markdown_by_stem.get(_collision_key(candidate.stem), [])) == 1
        return False
    return candidate in files


def _scan_links(root: Path, files: list[PurePosixPath]) -> list[str]:
    existing = set(files)
    markdown_by_stem: dict[str, list[PurePosixPath]] = {}
    for path in files:
        if path.suffix.casefold() == ".md":
            markdown_by_stem.setdefault(_collision_key(path.stem), []).append(path)
    errors: list[str] = []
    for relative in files:
        if relative.suffix.casefold() != ".md":
            continue
        try:
            contents = _text(root.joinpath(*relative.parts))
        except (OSError, UnicodeError):
            errors.append(f"{relative.as_posix()} -> unreadable")
            continue
        for match in _WIKILINK.finditer(contents):
            raw = match.group(1).split("|", 1)[0].strip()
            if not _resolve_local_target(relative, raw, existing, markdown_by_stem, markdown=True):
                errors.append(f"{relative.as_posix()} -> {raw}")
        for match in _IMAGE_LINK.finditer(contents):
            raw = match.group(1).strip()
            if not _resolve_local_target(relative, raw, existing, markdown_by_stem, markdown=False):
                errors.append(f"{relative.as_posix()} -> {raw}")
    return sorted(set(errors))


def _scan_secrets(root: Path, files: list[PurePosixPath]) -> list[str]:
    findings: list[str] = []
    text_suffixes = {".md", ".json", ".txt", ".yaml", ".yml", ".csv", ".html"}
    for relative in files:
        if relative.suffix.casefold() not in text_suffixes:
            continue
        try:
            contents = _text(root.joinpath(*relative.parts))
        except (OSError, UnicodeError):
            continue
        if any(pattern.search(contents) for pattern in (_SECRET_VALUE, _AUTHORIZATION, _COOKIE_HEADER, _ABSOLUTE_PATH)):
            findings.append(relative.as_posix())
    return sorted(findings)


def _manifest_report(root: Path, files: list[PurePosixPath]) -> tuple[list[str], int]:
    errors: list[str] = []
    manifest_path = root / "90-系统/manifest.json"
    try:
        data = json.loads(_text(manifest_path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["manifest is unreadable"], 0
    if not isinstance(data, dict) or set(data) != {"version", "articles"} or data.get("version") != 1 or not isinstance(data.get("articles"), dict):
        return ["manifest has unsupported schema"], 0
    articles = data["articles"]
    seen_urls: set[str] = set()
    seen_paths: set[str] = set()
    referenced_paths: set[PurePosixPath] = set()
    for key, record in articles.items():
        label = str(key)[:24]
        if not isinstance(key, str) or _HASH.fullmatch(key) is None or not isinstance(record, dict):
            errors.append(f"{label}: invalid key or record")
            continue
        if set(record) != _RECORD_FIELDS or record.get("key") != key:
            errors.append(f"{label}: invalid record fields")
            continue
        if any(not isinstance(record.get(field), str) for field in _FRONTMATTER_FIELDS):
            errors.append(f"{label}: invalid field type")
        source_url = record.get("source_url")
        try:
            canonical = canonical_url(source_url)
            if canonical != source_url or article_key(source_url) != key:
                errors.append(f"{label}: key and URL disagree")
        except (TypeError, ValueError):
            errors.append(f"{label}: invalid source URL")
            canonical = ""
        if canonical in seen_urls:
            errors.append(f"{label}: duplicate source URL")
        seen_urls.add(canonical)
        if _HASH.fullmatch(str(record.get("content_hash", ""))) is None:
            errors.append(f"{label}: invalid content hash")
        try:
            date.fromisoformat(str(record.get("published", "")))
            collected = datetime.fromisoformat(
                str(record.get("collected_at", "")).replace("Z", "+00:00")
            )
            if collected.tzinfo is None or collected.utcoffset() is None:
                raise ValueError
        except ValueError:
            errors.append(f"{label}: invalid article date")
        relative = _safe_relative(record.get("path"))
        if relative is None or len(relative.parts) < 3 or relative.parts[0] != "03-文章" or relative.suffix.casefold() != ".md":
            errors.append(f"{label}: unsafe article path")
            continue
        collision = _collision_key(relative.as_posix())
        if collision in seen_paths:
            errors.append(f"{label}: manifest path collision")
        seen_paths.add(collision)
        if key.removeprefix("sha256:")[:8] not in relative.stem.casefold():
            errors.append(f"{label}: article path disagrees with key")
        referenced_paths.add(relative)
        if relative not in set(files):
            errors.append(f"{label}: article file missing")
            continue
        frontmatter = _frontmatter(root.joinpath(*relative.parts))
        if frontmatter is None or set(frontmatter) != set(_FRONTMATTER_FIELDS):
            errors.append(f"{label}: invalid frontmatter")
        elif any(frontmatter[field] != record[field] for field in _FRONTMATTER_FIELDS):
            errors.append(f"{label}: frontmatter disagrees with manifest")
        attachments = record.get("attachments")
        safe_attachments = [_safe_relative(item) for item in attachments] if isinstance(attachments, list) else []
        if (
            not isinstance(attachments, list)
            or any(item is None or not item.parts or item.parts[0] != "04-附件" for item in safe_attachments)
        ):
            errors.append(f"{label}: invalid attachments")
        elif any(item not in set(files) for item in safe_attachments):
            errors.append(f"{label}: attachment file missing")
    article_files = {path for path in files if path.parts and path.parts[0] == "03-文章" and path.suffix.casefold() == ".md"}
    for orphan in sorted(article_files - referenced_paths):
        errors.append(f"{orphan.as_posix()}: orphan article")
    return sorted(set(errors)), len(articles)


def _status_report(root: Path, article_count: int) -> tuple[list[str], int, int]:
    errors: list[str] = []
    try:
        status = _text(root / "01-采集状态.md")
        report = _text(root / "90-系统/collection-report.md")
        home = _text(root / "00-首页.md")
    except (OSError, UnicodeError):
        return ["status or report is unreadable"], 0, 0
    numbers = {name: int(value) for name, value in _REPORT_NUMBER.findall(report)}
    if set(numbers) != {"项目数", "失败项目数", "文章总数"}:
        errors.append("report counters are missing")
        return errors, 0, 0
    if numbers["文章总数"] != article_count:
        errors.append("report article count disagrees with manifest")
    report_rows = [line for line in report.splitlines() if line.startswith("| ")][1:]
    status_rows = [line for line in status.splitlines() if line.startswith("| ")][1:]
    if report_rows != status_rows:
        errors.append("status and report project rows disagree")
    failed = 0
    for row in report_rows:
        cells: list[str] = []
        current: list[str] = []
        escaped = False
        for character in row.strip("|"):
            if escaped:
                current.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "|":
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        current.append("\\" if escaped else "")
        cells.append("".join(current).strip())
        if len(cells) < 8:
            errors.append("report contains malformed project row")
            continue
        try:
            failed_items = int(cells[5])
        except ValueError:
            errors.append("report contains invalid failure count")
            continue
        if failed_items > 0 or cells[6].casefold() not in {"success", "ok", "completed"}:
            failed += 1
    if len(report_rows) != numbers["项目数"] or failed != numbers["失败项目数"]:
        errors.append("report project counters disagree with rows")
    warning = "局部失败" in home
    if failed and not warning:
        errors.append("partial failure warning is missing")
    if not failed and warning:
        errors.append("home has a stale partial failure warning")
    return sorted(set(errors)), numbers["项目数"], failed


def lint_vault(vault: Path) -> dict[str, object]:
    root = Path(vault)
    files, directories, inventory_errors = _inventory(root)
    file_set = set(files)
    directory_set = set(directories)
    forbidden = list(inventory_errors)
    forbidden.extend(path.as_posix() for path in files + directories if _is_forbidden(path))
    for required in _REQUIRED_DIRECTORIES:
        if PurePosixPath(required) not in directory_set:
            forbidden.append(f"{required}: required directory missing")
    for required in _REQUIRED_FILES:
        if PurePosixPath(required) not in file_set:
            forbidden.append(f"{required}: required file missing")
    manifest_errors, article_count = _manifest_report(root, files)
    status_errors, project_count, failed_projects = _status_report(root, article_count)
    broken_links = _scan_links(root, files)
    secrets = _scan_secrets(root, files)
    report: dict[str, object] = {
        "broken_links": broken_links,
        "secrets": secrets,
        "forbidden_files": sorted(set(forbidden)),
        "manifest_errors": manifest_errors,
        "status_errors": status_errors,
        "article_count": article_count,
        "project_count": project_count,
        "failed_projects": failed_projects,
    }
    report["errors"] = sorted(
        broken_links + secrets + report["forbidden_files"] + manifest_errors + status_errors
    )
    return report


def _output_path(output: Path, now: datetime) -> Path:
    output = Path(output)
    if output.suffix.casefold() == ".zip":
        return output
    return output / f"英诺被投项目资讯库-{now:%Y%m%d-%H%M}.zip"


def build_delivery_zip(
    vault: Path,
    output: Path,
    *,
    now=lambda: datetime.now().astimezone(),
) -> dict[str, object]:
    root = Path(vault).resolve()
    destination = _output_path(Path(output), now())
    destination_parent = destination.parent.resolve()
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError:
        pass
    else:
        raise DeliveryValidationError({"errors": ["output must be outside vault"]})
    report = lint_vault(root)
    if report["errors"]:
        raise DeliveryValidationError(report)
    destination_parent.mkdir(parents=True, exist_ok=True)
    summary_path = destination.with_suffix(".summary.md")
    zip_temp: Path | None = None
    summary_temp: Path | None = None
    zip_installed = False
    summary_installed = False
    try:
        with tempfile.NamedTemporaryFile(dir=destination_parent, prefix=".delivery-", suffix=".tmp", delete=False) as handle:
            zip_temp = Path(handle.name)
        files, directories, inventory_errors = _inventory(root)
        if inventory_errors:
            raise DeliveryValidationError({"errors": inventory_errors})
        included_files = [path for path in files if path.as_posix() not in _SAFE_IGNORED_LOCKS]
        included_directories = [path for path in directories if path.as_posix() not in _SAFE_IGNORED_LOCKS]
        top = root.name
        with zipfile.ZipFile(zip_temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr(f"{top}/", b"")
            for relative in sorted(included_directories, key=lambda item: item.as_posix()):
                archive.writestr(f"{top}/{relative.as_posix()}/", b"")
            for relative in sorted(included_files, key=lambda item: item.as_posix()):
                if _is_forbidden(relative):
                    raise DeliveryValidationError({"errors": [relative.as_posix()]})
                payload = _open_regular(root.joinpath(*relative.parts))
                archive.writestr(f"{top}/{relative.as_posix()}", payload)
        digest = hashlib.sha256(zip_temp.read_bytes()).hexdigest()
        summary = (
            "# 交付摘要\n\n"
            f"- 文章数：{report['article_count']}\n"
            f"- 项目成功数：{int(report['project_count']) - int(report['failed_projects'])}\n"
            f"- 项目失败数：{report['failed_projects']}\n"
            f"- ZIP SHA-256：{digest}\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination_parent, prefix=".summary-", suffix=".tmp", delete=False) as handle:
            summary_temp = Path(handle.name)
            handle.write(summary)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(zip_temp, destination)
        zip_temp = None
        zip_installed = True
        os.replace(summary_temp, summary_path)
        summary_temp = None
        summary_installed = True
        return {
            "zip_path": destination,
            "summary_path": summary_path,
            "article_count": report["article_count"],
            "successful_projects": int(report["project_count"]) - int(report["failed_projects"]),
            "failed_projects": report["failed_projects"],
            "zip_sha256": digest,
        }
    except BaseException:
        if zip_installed and destination.exists():
            destination.unlink()
        if summary_installed and summary_path.exists():
            summary_path.unlink()
        raise
    finally:
        for temporary in (zip_temp, summary_temp):
            if temporary is not None and temporary.exists():
                temporary.unlink()
