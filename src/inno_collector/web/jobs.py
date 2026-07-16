from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..diagnostics import sanitize_diagnostic


_ACTIVE_STATUSES = {"queued", "running"}
_OUTCOME_STATUSES = {"succeeded", "partial"}
_EVENT_TYPES = {
    "project_started",
    "catalog_synced",
    "articles_selected",
    "download_progress",
    "project_finished",
    "validation_finished",
}
_BLOCKED_KEY = re.compile(
    r"(?i)(?:^|_)(?:path|dir|file|token|cookie|ticket|key|uuid|credential|secret)(?:$|_)"
)
_COUNT_KEYS = {
    "article_count",
    "project_count",
    "discovered",
    "selected",
    "downloaded",
    "skipped",
    "failed",
    "failed_projects",
}


class JobBusyError(RuntimeError):
    pass


class JobGoneError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: str
    result: dict[str, object]

    def __post_init__(self) -> None:
        if self.status not in _OUTCOME_STATUSES:
            raise ValueError("invalid job outcome status")


@dataclass(slots=True)
class _Job:
    id: str
    kind: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, object] = field(default_factory=dict)
    error: str = ""
    events: list[dict[str, object]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)


def _safe_value(value: object, depth: int = 0) -> object:
    if depth > 5:
        return None
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, str):
        return sanitize_diagnostic(value, fallback="")[:512]
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item, depth + 1)
            for key, item in list(value.items())[:100]
            if not _BLOCKED_KEY.search(str(key))
        }
    return sanitize_diagnostic(value, fallback="")[:512]


class JobContext:
    def __init__(self, manager: JobManager, job_id: str) -> None:
        self._manager = manager
        self.job_id = job_id

    def is_cancelled(self) -> bool:
        return self._manager._is_cancelled(self.job_id)

    def checkpoint(self) -> None:
        if self.is_cancelled():
            raise JobCancelled

    def emit(
        self,
        event_type: str,
        *,
        project: str = "",
        stage: str = "",
        counts: dict[str, int] | None = None,
    ) -> None:
        self._manager._emit(
            self.job_id,
            event_type,
            project=project,
            stage=stage,
            counts=counts or {},
        )


Operation = Callable[[JobContext], dict[str, object] | JobOutcome | None]


class JobManager:
    def __init__(
        self,
        *,
        max_completed: int = 100,
        max_age_seconds: float = 24 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_completed) is not int or max_completed < 1:
            raise ValueError("max_completed must be positive")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self.max_completed = max_completed
        self.max_age_seconds = float(max_age_seconds)
        self.clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._completed: list[str] = []
        self._active_id: str | None = None

    def _cleanup_locked(self) -> None:
        now = self.clock()
        retained: list[str] = []
        for job_id in self._completed:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job.finished_at is None
                or now - job.finished_at > self.max_age_seconds
            ):
                self._jobs.pop(job_id, None)
            else:
                retained.append(job_id)
        while len(retained) > self.max_completed:
            self._jobs.pop(retained.pop(0), None)
        self._completed = retained

    def submit(self, kind: str, operation: Operation) -> str:
        if kind not in {"preflight", "collection", "delivery"}:
            raise ValueError("invalid job kind")
        if not callable(operation):
            raise TypeError("operation must be callable")
        with self._lock:
            self._cleanup_locked()
            if self._active_id is not None:
                active = self._jobs.get(self._active_id)
                if active is not None and active.status in _ACTIVE_STATUSES:
                    raise JobBusyError("another write job is active")
            job_id = secrets.token_urlsafe(24)
            while job_id in self._jobs:
                job_id = secrets.token_urlsafe(24)
            job = _Job(
                id=job_id,
                kind=kind,
                status="queued",
                created_at=self.clock(),
            )
            self._jobs[job_id] = job
            self._active_id = job_id
        thread = threading.Thread(
            target=self._run,
            args=(job_id, operation),
            name=f"inno-job-{kind}",
            daemon=True,
        )
        thread.start()
        return job_id

    def _run(self, job_id: str, operation: Operation) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = self.clock()
        context = JobContext(self, job_id)
        try:
            context.checkpoint()
            raw_outcome = operation(context)
            context.checkpoint()
            if isinstance(raw_outcome, JobOutcome):
                status = raw_outcome.status
                result = raw_outcome.result
            else:
                status = "succeeded"
                result = raw_outcome if isinstance(raw_outcome, dict) else {}
            safe_result = _safe_value(result)
            if not isinstance(safe_result, dict):
                safe_result = {}
            error = ""
        except JobCancelled:
            status = "cancelled"
            safe_result = {}
            error = "任务已取消。"
        except Exception:
            status = "failed"
            safe_result = {}
            error = "任务执行失败。"
        with self._lock:
            job = self._jobs[job_id]
            job.status = status
            job.result = safe_result
            job.error = error
            job.finished_at = self.clock()
            job.done_event.set()
            if self._active_id == job_id:
                self._active_id = None
            self._completed.append(job_id)
            self._cleanup_locked()

    def _job_locked(self, job_id: str) -> _Job:
        self._cleanup_locked()
        job = self._jobs.get(job_id)
        if job is None:
            raise JobGoneError("job is no longer available")
        return job

    def _snapshot_locked(self, job: _Job) -> dict[str, object]:
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "result": dict(job.result),
            "error": job.error,
            "cancel_requested": job.cancel_event.is_set(),
        }

    def get(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._snapshot_locked(self._job_locked(job_id))

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, object]:
        with self._lock:
            job = self._job_locked(job_id)
            done = job.done_event
        if not done.wait(timeout):
            raise TimeoutError("job did not finish")
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._job_locked(job_id)
            if job.status in _ACTIVE_STATUSES:
                job.cancel_event.set()
            return self._snapshot_locked(job)

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return self._job_locked(job_id).cancel_event.is_set()

    def _emit(
        self,
        job_id: str,
        event_type: str,
        *,
        project: str,
        stage: str,
        counts: dict[str, int],
    ) -> None:
        if event_type not in _EVENT_TYPES:
            raise ValueError("invalid job event type")
        safe_counts = {
            key: value
            for key, value in counts.items()
            if key in _COUNT_KEYS and type(value) is int and value >= 0
        }
        with self._lock:
            job = self._job_locked(job_id)
            job.events.append(
                {
                    "sequence": len(job.events) + 1,
                    "type": event_type,
                    "project": sanitize_diagnostic(project, fallback="")[:160],
                    "stage": sanitize_diagnostic(stage, fallback="")[:80],
                    "counts": safe_counts,
                    "at": self.clock(),
                }
            )
            if len(job.events) > 1000:
                del job.events[: len(job.events) - 1000]

    def events(self, job_id: str, after: int = 0) -> dict[str, object]:
        if type(after) is not int or after < 0:
            raise ValueError("after must be a non-negative integer")
        with self._lock:
            job = self._job_locked(job_id)
            events = [dict(event) for event in job.events if event["sequence"] > after]
            next_sequence = job.events[-1]["sequence"] if job.events else after
            return {"events": events, "next": next_sequence}
