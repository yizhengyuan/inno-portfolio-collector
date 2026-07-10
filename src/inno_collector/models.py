from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectAccount:
    project: str
    account: str
    wechat_id: str = ""
    confidence: str = "high"
    enabled: bool = True
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NormalizedArticle:
    key: str
    project: str
    account: str
    title: str
    published: str
    source_url: str
    collected_at: str
    content_hash: str
    body: str
    source_markdown: Path
    source_image_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class RejectedArticle:
    title: str
    source_url: str
    reason: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    valid: tuple[NormalizedArticle, ...]
    rejected: tuple[RejectedArticle, ...]


@dataclass(frozen=True, slots=True)
class ProjectRunResult:
    project: str
    account: str
    discovered: int
    downloaded: int
    skipped: int
    failed: int
    status: str
    error: str
    last_sync: str = ""


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    projects: tuple[ProjectRunResult, ...]
    project_count: int
    failed_projects: int
    article_count: int
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class VaultApplyResult:
    created: int
    updated: int
    unchanged: int
