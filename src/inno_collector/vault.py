from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .ingest import markdown_image_destinations, yaml_string
from .models import NormalizedArticle, ProjectRunResult, VaultApplyResult
from .state import ManifestStore


class AttachmentSyncError(RuntimeError):
    pass


class ManifestPathCollisionError(RuntimeError):
    pass


_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\[\]#^]')
_SHA256_KEY = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_PUBLISHED_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ARTICLE_BODY = re.compile(r"\A---\n.*?\n---\n\n", re.DOTALL)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_ARTICLE_METADATA_FIELDS = (
    "project",
    "account",
    "title",
    "published",
    "source_url",
    "collected_at",
    "content_hash",
)
_YAML_TYPED_SCALARS = {
    "null",
    "true",
    "false",
    "yes",
    "no",
    "on",
    "off",
    "~",
}
_ABSOLUTE_PATH = re.compile(
    r"(?<![:\w])/(?:Users|private|var|tmp)/[^\s|]+|"
    r"(?i:(?<![\w])(?:[A-Z]:\\)[^\s|]+)"
)


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


def _identity_digest(key: str) -> str:
    match = _SHA256_KEY.fullmatch(key)
    if match is not None:
        return match.group(1).lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_suffixes(keys: object) -> dict[str, str]:
    ordered_keys = sorted(set(str(key) for key in keys))
    digests = {key: _identity_digest(key) for key in ordered_keys}
    groups: dict[str, list[str]] = {}
    for key in ordered_keys:
        groups.setdefault(digests[key][:8], []).append(key)

    suffixes: dict[str, str] = {}
    for group in groups.values():
        if len(group) == 1:
            suffixes[group[0]] = digests[group[0]][:8]
            continue
        for key in group:
            digest = digests[key]
            for length in range(9, len(digest) + 1):
                if sum(
                    digests[other].startswith(digest[:length])
                    for other in group
                ) == 1:
                    suffixes[key] = digest[:length]
                    break
            else:
                identical = [other for other in group if digests[other] == digest]
                key_hashes = {
                    other: hashlib.sha256(other.encode("utf-8")).hexdigest()
                    for other in identical
                }
                for length in range(8, 65):
                    if sum(
                        value.startswith(key_hashes[key][:length])
                        for value in key_hashes.values()
                    ) == 1:
                        suffixes[key] = f"{digest}-{key_hashes[key][:length]}"
                        break
                else:
                    raise ValueError("unable to allocate unique key suffix")
    return suffixes


def _key_suffix(key: str) -> str:
    return _key_suffixes([key])[key]


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


def _remove_directory(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ValueError("unsafe attachment directory")
    shutil.rmtree(path)


def _files_equal(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as first_handle, second.open("rb") as second_handle:
            while True:
                first_chunk = first_handle.read(1 << 20)
                second_chunk = second_handle.read(1 << 20)
                if first_chunk != second_chunk:
                    return False
                if not first_chunk:
                    return True
    except OSError:
        return False


def _directory_snapshots_equal(first: Path, second: Path) -> bool:
    def regular_files(root: Path) -> dict[PurePosixPath, Path] | None:
        files: dict[PurePosixPath, Path] = {}
        try:
            paths = list(root.rglob("*"))
        except OSError:
            return None
        for path in paths:
            try:
                details = path.lstat()
            except OSError:
                return None
            if stat.S_ISLNK(details.st_mode):
                return None
            if stat.S_ISREG(details.st_mode):
                files[PurePosixPath(*path.relative_to(root).parts)] = path
            elif not stat.S_ISDIR(details.st_mode):
                return None
        return files

    first_files = regular_files(first)
    second_files = regular_files(second)
    if first_files is None or second_files is None:
        return False
    if set(first_files) != set(second_files):
        return False
    return all(
        _files_equal(first_files[relative], second_files[relative])
        for relative in first_files
    )


def _new_backup_path(parent: Path, asset_name: str) -> Path:
    digest = hashlib.sha256(asset_name.encode("utf-8")).hexdigest()[:12]
    candidate = parent / f".v.backup-{digest}-{uuid.uuid4().hex[:12]}"
    try:
        candidate.lstat()
    except FileNotFoundError:
        return candidate
    raise ValueError("unsafe attachment backup")


def _remove_regular_file(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError("unsafe article file")
    path.unlink()


class _AttachmentCommit:
    def __init__(
        self,
        final_directory: Path,
        backup_directory: Path | None,
        final_present: bool,
        parent_created: bool,
    ) -> None:
        self.final_directory = final_directory
        self.backup_directory = backup_directory
        self.final_present = final_present
        self.parent_created = parent_created

    def rollback(self) -> None:
        if self.final_present:
            _remove_directory(self.final_directory)
            self.final_present = False
        if self.backup_directory is not None:
            os.replace(self.backup_directory, self.final_directory)
            self.backup_directory = None
        if self.parent_created:
            try:
                self.final_directory.parent.rmdir()
            except OSError:
                pass
            self.parent_created = False

    def finalize(self) -> None:
        if self.backup_directory is not None:
            _remove_directory(self.backup_directory)
            self.backup_directory = None


class _ArticleCommit:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.backup_path: Path | None = None
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ValueError("unsafe article file")
        self.backup_path = _new_backup_path(path.parent, path.name + ".article")
        os.replace(path, self.backup_path)

    def rollback(self) -> None:
        _remove_regular_file(self.path)
        if self.backup_path is not None:
            os.replace(self.backup_path, self.path)
            self.backup_path = None

    def finalize(self) -> None:
        if self.backup_path is not None:
            _remove_regular_file(self.backup_path)
            self.backup_path = None


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
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
        if not line.startswith("read_status:"):
            continue
        for following in lines[index + 1 :]:
            if following.strip() == "---":
                break
            if not following.strip():
                continue
            if following.startswith((" ", "\t")):
                return fallback
            break
        raw_value = line.partition(":")[2].strip()
        try:
            value = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            return _plain_read_status(raw_value, fallback)
        return value if isinstance(value, str) else fallback
    return fallback


def _plain_read_status(value: str, fallback: str) -> str:
    if (
        not value
        or len(value) > 64
        or value.casefold() in _YAML_TYPED_SCALARS
        or re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value) is not None
        or any(
            character in "#[]{}|>&*!,:`\"'"
            or unicodedata.category(character) == "Cc"
            for character in value
        )
    ):
        return fallback
    return value


def _read_article_metadata(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0] != "---":
        return None
    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        name, separator, raw_value = line.partition(": ")
        if not separator or name not in _ARTICLE_METADATA_FIELDS:
            continue
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, str):
            return None
        metadata[name] = value
    if set(metadata) != set(_ARTICLE_METADATA_FIELDS):
        return None
    return metadata


def _read_article_body(path: Path) -> str | None:
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _ARTICLE_BODY.match(contents)
    if match is None:
        return None
    return contents[match.end() :]


def _plain_cell(value: object) -> str:
    return _external_text(value).replace("|", "\\|")


def _external_text(value: object) -> str:
    text = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in str(value)
    )
    text = " ".join(text.split())
    return html.escape(text, quote=False)


def _wiki_alias(value: object) -> str:
    return (
        _external_text(value)
        .replace("|", "｜")
        .replace("[", "［")
        .replace("]", "］")
    )


def _redact_error(value: object) -> str:
    return _ABSOLUTE_PATH.sub("[path]", str(value))


def _table(project_results: list[ProjectRunResult]) -> str:
    rows = [
        "| project | account | discovered | downloaded | skipped | failed | "
        "status | error | last_sync |",
        "|---|---|---:|---:|---:|---:|---|---|---|",
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
                    _plain_cell(_redact_error(result.error)),
                    _plain_cell(result.last_sync),
                )
            )
            + " |"
        )
    return "\n".join(rows)


def _project_page_stems(projects: list[str]) -> dict[str, str]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for project in projects:
        base = _safe_filename(project, "未命名项目", 80)
        collision_key = unicodedata.normalize("NFC", base).casefold()
        grouped.setdefault(collision_key, []).append((project, base))

    stems: dict[str, str] = {}
    for entries in grouped.values():
        ordered = sorted(
            entries,
            key=lambda item: (
                unicodedata.normalize("NFC", item[0]).casefold(),
                unicodedata.normalize("NFC", item[0]),
                item[0],
            ),
        )
        if len(ordered) == 1:
            project, base = ordered[0]
            stems[project] = base
            continue
        for index, (project, base) in enumerate(ordered, start=1):
            digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:8]
            stems[project] = f"{base}-{index:02d}-{digest}"
    return stems


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

    def _new_article_path(
        self, article: NormalizedArticle, suffix: str | None = None
    ) -> PurePosixPath:
        return self._new_record_path(
            article.key,
            article.project,
            article.title,
            article.published,
            suffix,
        )

    def _new_record_path(
        self,
        key: str,
        project_value: object,
        title_value: object,
        published_value: object,
        suffix: str | None = None,
    ) -> PurePosixPath:
        project = _safe_filename(str(project_value), "未命名项目", 80)
        title = _safe_filename(str(title_value), "未命名文章", 96)
        published_text = str(published_value)
        published = (
            published_text
            if _PUBLISHED_DATE.fullmatch(published_text)
            else "0000-00-00"
        )
        filename = f"{published}-{title}-{suffix or _key_suffix(key)}.md"
        return PurePosixPath("03-文章", project, filename)

    def _path_collision_key(self, path: PurePosixPath) -> str:
        return unicodedata.normalize("NFC", path.as_posix()).casefold()

    def _allocated_record_path(
        self,
        key: str,
        record: dict[str, object],
        occupied: set[str],
        suffix: str | None = None,
    ) -> PurePosixPath:
        candidate = self._new_record_path(
            key,
            record.get("project", ""),
            record.get("title", ""),
            record.get("published", ""),
            suffix,
        )
        collision_key = self._path_collision_key(candidate)
        if collision_key not in occupied:
            occupied.add(collision_key)
            return candidate
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        for length in range(12, len(digest) + 1, 4):
            extended = candidate.with_name(
                f"{candidate.stem}-{digest[:length]}{candidate.suffix}"
            )
            collision_key = self._path_collision_key(extended)
            if collision_key not in occupied:
                occupied.add(collision_key)
                return extended
        raise ValueError("unable to allocate unique article path")

    def _resolve_manifest_path_collisions(
        self,
        store: ManifestStore,
        suffixes: dict[str, str],
        incoming_keys: set[str],
    ) -> set[PurePosixPath]:
        groups: dict[str, list[str]] = {}
        for key, record in store.data["articles"].items():
            path = _article_relative_path(record.get("path"))
            if path is not None:
                groups.setdefault(self._path_collision_key(path), []).append(key)
        collision_groups = [keys for keys in groups.values() if len(keys) > 1]
        if any(not incoming_keys.intersection(keys) for keys in collision_groups):
            raise ManifestPathCollisionError(
                "manifest path collision requires refetch"
            )
        occupied = {
            collision_key
            for collision_key, keys in groups.items()
            if len(keys) == 1
        }
        stale_paths: set[PurePosixPath] = set()
        for keys in collision_groups:
            incoming = incoming_keys.intersection(keys)
            if len(incoming) < len(keys):
                existing_path = _article_relative_path(
                    store.data["articles"][keys[0]].get("path")
                )
                assert existing_path is not None
                occupied.add(self._path_collision_key(existing_path))
            else:
                stale_paths.update(
                    path
                    for key in keys
                    if (
                        path := _article_relative_path(
                            store.data["articles"][key].get("path")
                        )
                    )
                    is not None
                )
            for key in sorted(incoming):
                record = store.data["articles"][key]
                record["path"] = self._allocated_record_path(
                    key, record, occupied, suffixes.get(key)
                ).as_posix()
        return stale_paths

    def _attachment_root(
        self, article: NormalizedArticle, suffix: str | None = None
    ) -> PurePosixPath:
        project = _safe_filename(article.project, "未命名项目", 80)
        title = _safe_filename(article.title, "未命名文章", 80)
        return PurePosixPath(
            "04-附件", project, f"{title}-{suffix or _key_suffix(article.key)}"
        )

    def _copy_attachments(
        self, article: NormalizedArticle, suffix: str | None = None
    ) -> tuple[
        list[str],
        dict[str, PurePosixPath],
        _AttachmentCommit | None,
        str | None,
    ]:
        source = article.source_image_dir
        if source is None:
            return [], {}, None, None
        source_path = Path(source)
        try:
            if source_path.is_symlink() or not source_path.is_dir():
                return [], {}, None, "附件源暂不可用"
            source_root = source_path.resolve(strict=True)
        except (OSError, RuntimeError):
            return [], {}, None, "附件源暂不可用"

        attachment_root = self._attachment_root(article, suffix)
        final_directory = self._path(attachment_root)
        project_directory = self._path(attachment_root.parent)
        project_directory_created = not project_directory.exists()
        project_directory.mkdir(parents=True, exist_ok=True)
        try:
            final_details = final_directory.lstat()
        except FileNotFoundError:
            final_exists = False
        else:
            if stat.S_ISLNK(final_details.st_mode) or not stat.S_ISDIR(
                final_details.st_mode
            ):
                raise ValueError("unsafe attachment directory")
            final_exists = True

        stage_directory: Path | None = Path(
            tempfile.mkdtemp(
                dir=project_directory,
                prefix=(
                    ".v.stage-"
                    + hashlib.sha256(
                        attachment_root.name.encode("utf-8")
                    ).hexdigest()[:12]
                    + "-"
                ),
            )
        )
        backup_directory: Path | None = None
        copied: list[str] = []
        mapping: dict[str, PurePosixPath] = {}
        walk_errors: list[OSError] = []
        try:
            assert stage_directory is not None
            for directory, directory_names, filenames in os.walk(
                source_root,
                topdown=True,
                onerror=walk_errors.append,
                followlinks=False,
            ):
                current = Path(directory)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if not name.startswith(".")
                    and not (current / name).is_symlink()
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
                    except (OSError, RuntimeError):
                        return [], {}, None, "附件文件不可读"
                    except ValueError:
                        continue
                    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(
                        details.st_mode
                    ):
                        continue
                    relative_source = resolved.relative_to(source_root)
                    relative_posix = PurePosixPath(*relative_source.parts)
                    destination_relative = attachment_root / relative_posix
                    stage_destination = stage_directory.joinpath(
                        *relative_posix.parts
                    )
                    try:
                        _atomic_copy(resolved, stage_destination)
                    except OSError:
                        return [], {}, None, "附件复制失败"
                    destination_text = destination_relative.as_posix()
                    copied.append(destination_text)
                    mapping[relative_posix.as_posix()] = destination_relative

            if walk_errors:
                return [], {}, None, "附件目录不可读"

            if copied and final_exists and _directory_snapshots_equal(
                stage_directory, final_directory
            ):
                _remove_directory(stage_directory)
                stage_directory = None
                return sorted(copied), mapping, None, None

            if not copied:
                _remove_directory(stage_directory)
                stage_directory = None
                if final_exists:
                    backup_directory = _new_backup_path(
                        project_directory, attachment_root.name
                    )
                    os.replace(final_directory, backup_directory)
                    commit = _AttachmentCommit(
                        final_directory,
                        backup_directory,
                        final_present=False,
                        parent_created=project_directory_created,
                    )
                    backup_directory = None
                    return [], {}, commit, None
                return [], {}, None, None

            if final_exists:
                backup_directory = _new_backup_path(
                    project_directory, attachment_root.name
                )
                os.replace(final_directory, backup_directory)
            try:
                os.replace(stage_directory, final_directory)
                stage_directory = None
            except BaseException:
                if backup_directory is not None:
                    os.replace(backup_directory, final_directory)
                    backup_directory = None
                raise
            commit = _AttachmentCommit(
                final_directory,
                backup_directory,
                final_present=True,
                parent_created=project_directory_created,
            )
            backup_directory = None
            return sorted(copied), mapping, commit, None
        finally:
            if stage_directory is not None:
                _remove_directory(stage_directory)
            if backup_directory is not None and not final_directory.exists():
                os.replace(backup_directory, final_directory)
            if project_directory_created:
                try:
                    project_directory.rmdir()
                except OSError:
                    pass

    def _rewrite_links(
        self,
        body: str,
        mapping: dict[str, PurePosixPath],
    ) -> str:
        if not mapping:
            return body

        replacements: list[tuple[int, int, str]] = []
        for start, end, raw_target in markdown_image_destinations(body):
            if raw_target.casefold().startswith(("http://", "https://")):
                continue
            decoded = unquote(raw_target)
            target = PurePosixPath(decoded)
            if (
                len(target.parts) < 4
                or target.parts[:2] != ("..", "images")
                or target.parts[2] in {"", ".", ".."}
                or any(part in {"", ".", ".."} for part in target.parts[3:])
            ):
                continue
            source_relative = PurePosixPath(*target.parts[3:]).as_posix()
            copied = mapping.get(source_relative)
            if copied is None:
                continue
            rewritten = (PurePosixPath("..", "..") / copied).as_posix()
            for literal, encoded in (
                ("%", "%25"),
                (" ", "%20"),
                ("(", "%28"),
                (")", "%29"),
                ("#", "%23"),
                ("?", "%3F"),
            ):
                rewritten = rewritten.replace(literal, encoded)
            rewritten = "".join(
                "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
                if character.isspace()
                else character
                for character in rewritten
            )
            replacements.append((start, end, rewritten))

        rewritten_body = body
        for start, end, replacement in reversed(replacements):
            rewritten_body = (
                rewritten_body[:start] + replacement + rewritten_body[end:]
            )
        return rewritten_body

    def _has_exporter_image_reference(self, body: str) -> bool:
        for _, _, raw_target in markdown_image_destinations(body):
            if raw_target.casefold().startswith(("http://", "https://")):
                continue
            target = PurePosixPath(unquote(raw_target))
            if (
                len(target.parts) >= 4
                and target.parts[:2] == ("..", "images")
                and target.parts[2] not in {"", ".", ".."}
                and all(part not in {"", ".", ".."} for part in target.parts[3:])
            ):
                return True
        return False

    def _attachment_mapping(
        self, attachments: list[str]
    ) -> dict[str, PurePosixPath]:
        mapping: dict[str, PurePosixPath] = {}
        for attachment in attachments:
            relative = _safe_relative_path(attachment)
            if (
                relative is None
                or len(relative.parts) < 4
                or relative.parts[0] != "04-附件"
            ):
                continue
            source_relative = PurePosixPath(*relative.parts[3:]).as_posix()
            mapping[source_relative] = relative
        return mapping

    def _attachment_roots(self, attachments: object) -> set[PurePosixPath]:
        roots: set[PurePosixPath] = set()
        if not isinstance(attachments, list):
            return roots
        for attachment in attachments:
            relative = _safe_relative_path(attachment)
            if (
                relative is not None
                and len(relative.parts) >= 4
                and relative.parts[0] == "04-附件"
            ):
                roots.add(PurePosixPath(*relative.parts[:3]))
        return roots

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

    def _sanitize_manifest(
        self,
        store: ManifestStore,
        suffixes: dict[str, str],
    ) -> None:
        cleaned_records: dict[str, dict[str, object]] = {}
        for key, record in store.data["articles"].items():
            path = _article_relative_path(record.get("path"))
            if path is None:
                path = self._new_record_path(
                    key,
                    record.get("project", ""),
                    record.get("title", ""),
                    record.get("published", ""),
                    suffixes.get(key),
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
        attachment_warnings: list[str],
    ) -> None:
        sorted_results = sorted(
            project_results,
            key=lambda result: (
                result.project,
                result.account,
                result.status,
                result.discovered,
                result.downloaded,
                result.skipped,
                result.failed,
                result.error,
                result.last_sync,
            ),
        )
        records = store.data["articles"]
        projects = {
            result.project
            for result in sorted_results
            if isinstance(result.project, str)
        }
        projects.update(
            record.get("project")
            for record in records.values()
            if isinstance(record.get("project"), str)
        )
        project_names = sorted(projects, key=lambda value: (value.casefold(), value))
        project_page_stems = _project_page_stems(project_names)
        expected_project_pages: set[str] = set()

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
            lines = [f"# {_wiki_alias(project)}", ""]
            if not articles:
                lines.append("暂无文章。")
            for _, record in articles:
                relative = _article_relative_path(record["path"])
                assert relative is not None
                target = PurePosixPath("..") / relative
                title = _wiki_alias(record.get("title", "未命名文章"))
                published = _plain_cell(record.get("published", ""))
                lines.append(f"- {published} [[{target.as_posix()}|{title}]]")
            page_name = project_page_stems[project] + ".md"
            expected_project_pages.add(page_name)
            _atomic_write(
                self._path(PurePosixPath("02-项目", page_name)),
                ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
            )

        project_directory = self._path("02-项目")
        for entry in project_directory.iterdir():
            if entry.name in expected_project_pages or entry.suffix.casefold() != ".md":
                continue
            try:
                details = entry.lstat()
            except OSError:
                continue
            if stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                entry.unlink()

        last_sync = max(
            [
                str(record.get("collected_at", ""))
                for record in records.values()
                if str(record.get("collected_at", ""))
            ]
            + [result.last_sync for result in sorted_results if result.last_sync],
            default="无",
        )
        failed_projects = sum(
            result.failed > 0
            or result.status.strip().casefold() not in {"success", "ok", "completed"}
            for result in sorted_results
        )
        home_lines = [
            "# 英诺项目文章库",
            "",
            "[[01-采集状态|采集状态]]",
            "",
            f"总文章数：{len(records)}",
            f"最近更新时间：{_external_text(last_sync)}",
            "",
        ]
        if failed_projects or attachment_warnings:
            home_lines.extend(
                ["> ⚠️ 本次采集存在局部失败，请查看采集状态与报告。", ""]
            )
        recent_articles = sorted(
            (
                (key, record)
                for key, record in records.items()
                if _article_relative_path(record.get("path")) is not None
            ),
            key=lambda item: (
                str(item[1].get("published", "")),
                str(item[1].get("collected_at", "")),
                item[0],
            ),
            reverse=True,
        )[:5]
        home_lines.extend(["## 最近文章", ""])
        if not recent_articles:
            home_lines.append("暂无文章。")
        for _, record in recent_articles:
            relative = _article_relative_path(record.get("path"))
            assert relative is not None
            home_lines.append(
                f"- [[{relative.as_posix()}|{_wiki_alias(record.get('title', '未命名文章'))}]]"
            )
        home_lines.extend(["", "## 项目", ""])
        home_lines.extend(
            f"- [[02-项目/{project_page_stems[project]}|"
            f"{_wiki_alias(project)}]]"
            for project in project_names
        )
        _atomic_write(
            self._path("00-首页.md"),
            ("\n".join(home_lines).rstrip() + "\n").encode("utf-8"),
        )

        status = (
            "# 采集状态\n\n"
            f"最后同步时间：{_external_text(last_sync)}\n\n"
            + _table(sorted_results)
            + "\n"
        )
        _atomic_write(self._path("01-采集状态.md"), status.encode("utf-8"))

        report = (
            "# 本次采集报告\n\n"
            f"- 项目数：{len(sorted_results)}\n"
            f"- 失败项目数：{failed_projects}\n"
            f"- 文章总数：{len(records)}\n\n"
            "## 项目统计\n\n"
            + _table(sorted_results)
            + "\n"
        )
        if attachment_warnings:
            report += "\n## 附件警告\n\n" + "\n".join(
                f"- {_plain_cell(warning)}"
                for warning in sorted(set(attachment_warnings))
            ) + "\n"
        _atomic_write(
            self._path("90-系统/collection-report.md"), report.encode("utf-8")
        )
        readme = (
            "# 使用说明\n\n"
            "请在 Obsidian 中将本目录作为仓库打开。\n\n"
            "文章 frontmatter 中的 `read_status` 默认为 `unread`，"
            "可人工修改；"
            "后续内容更新会优先保留该值。\n\n"
            "`02-项目` 由系统根据 manifest 管理；其中过期的系统生成 Markdown "
            "页面会在同步时清理，请勿在该目录保存手工 `.md` 文件。\n\n"
            "`90-系统` 保存 manifest、采集报告和本说明，"
            "请勿随意删除。\n"
        )
        _atomic_write(self._path("90-系统/README.md"), readme.encode("utf-8"))

    def apply(
        self,
        articles: list[NormalizedArticle],
        project_results: list[ProjectRunResult],
    ) -> VaultApplyResult:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".vault.lock"
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._apply_locked(articles, project_results)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _apply_locked(
        self,
        articles: list[NormalizedArticle],
        project_results: list[ProjectRunResult],
    ) -> VaultApplyResult:
        for relative in ("02-项目", "03-文章", "04-附件", "90-系统"):
            self._path(relative).mkdir(parents=True, exist_ok=True)
        store = ManifestStore(self._path("90-系统/manifest.json"))
        incoming_keys = {article.key for article in articles}
        suffixes = _key_suffixes(
            set(store.data["articles"]) | incoming_keys
        )
        stale_article_paths = self._resolve_manifest_path_collisions(
            store, suffixes, incoming_keys
        )
        created = updated = unchanged = 0
        seen: set[str] = set()
        stale_attachment_roots: set[PurePosixPath] = set()
        attachment_warnings: list[str] = []
        committed_updates: list[
            tuple[_ArticleCommit | None, _AttachmentCommit | None]
        ] = []

        try:
            for article in articles:
                if article.key in seen:
                    continue
                seen.add(article.key)
                article_commit: _ArticleCommit | None = None
                attachment_commit: _AttachmentCommit | None = None
                try:
                    existing = store.get(article.key)
                    old_attachment_roots = self._attachment_roots(
                        None if existing is None else existing.get("attachments")
                    )
                    relative = (
                        None
                        if existing is None
                        else _article_relative_path(existing.get("path"))
                    )
                    if relative is None:
                        relative = self._new_article_path(
                            article, suffixes[article.key]
                        )
                    destination = self._path(relative)
                    read_status = "unread"
                    if existing is not None and isinstance(
                        existing.get("read_status"), str
                    ):
                        read_status = existing["read_status"]
                    if destination.is_file():
                        read_status = _read_status(destination, read_status)

                    (
                        attachments,
                        attachment_mapping,
                        attachment_commit,
                        attachment_warning,
                    ) = self._copy_attachments(article, suffixes[article.key])
                    if attachment_warning is not None:
                        attachments = (
                            []
                            if existing is None
                            else self._safe_attachments(existing.get("attachments"))
                        )
                        if not attachments:
                            raise AttachmentSyncError(
                                "attachment sync failed without previous snapshot"
                            )
                        attachment_mapping = self._attachment_mapping(attachments)
                        attachment_warnings.append(
                            f"{article.project} / {article.title}：{attachment_warning}，"
                            "已保留已有附件快照"
                        )
                    elif article.source_image_dir is None and existing is not None:
                        attachments = self._safe_attachments(
                            existing.get("attachments")
                        )
                        attachment_mapping = self._attachment_mapping(attachments)
                    body = self._rewrite_links(article.body, attachment_mapping)
                    if self._has_exporter_image_reference(body):
                        raise AttachmentSyncError(
                            "unresolved local image references"
                        )
                    stale_attachment_roots.update(
                        old_attachment_roots - self._attachment_roots(attachments)
                    )
                    rendered_body = body.lstrip("\n")
                    existing_body = (
                        _read_article_body(destination)
                        if destination.is_file()
                        else None
                    )
                    existing_metadata = (
                        _read_article_metadata(destination)
                        if destination.is_file()
                        else None
                    )
                    expected_metadata = {
                        field: getattr(article, field)
                        for field in _ARTICLE_METADATA_FIELDS
                    }

                    same_hash = (
                        existing is not None
                        and existing.get("content_hash") == article.content_hash
                        and destination.is_file()
                        and existing_body == rendered_body
                        and existing_metadata == expected_metadata
                    )
                    if same_hash:
                        unchanged += 1
                    else:
                        article_commit = _ArticleCommit(destination)
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
                except BaseException:
                    if article_commit is not None:
                        article_commit.rollback()
                    if attachment_commit is not None:
                        attachment_commit.rollback()
                    raise
                committed_updates.append((article_commit, attachment_commit))

            self._sanitize_manifest(store, suffixes)
            stale_article_paths.update(
                self._resolve_manifest_path_collisions(
                    store, suffixes, incoming_keys
                )
            )
            store.save()
        except BaseException:
            for article_commit, attachment_commit in reversed(committed_updates):
                if article_commit is not None:
                    article_commit.rollback()
                if attachment_commit is not None:
                    attachment_commit.rollback()
            raise

        for article_commit, attachment_commit in committed_updates:
            if article_commit is not None:
                article_commit.finalize()
            if attachment_commit is not None:
                attachment_commit.finalize()
        active_article_paths = {
            self._path_collision_key(path)
            for record in store.data["articles"].values()
            if (path := _article_relative_path(record.get("path"))) is not None
        }
        for stale_path in sorted(stale_article_paths, key=PurePosixPath.as_posix):
            if self._path_collision_key(stale_path) in active_article_paths:
                continue
            destination = self._path(stale_path)
            try:
                details = destination.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                destination.unlink()
        active_attachment_roots: set[PurePosixPath] = set()
        for record in store.data["articles"].values():
            active_attachment_roots.update(
                self._attachment_roots(record.get("attachments"))
            )
        for stale_root in sorted(
            stale_attachment_roots - active_attachment_roots,
            key=PurePosixPath.as_posix,
        ):
            _remove_directory(self._path(stale_root))
        self._write_indexes(store, project_results, attachment_warnings)
        return VaultApplyResult(created, updated, unchanged)
