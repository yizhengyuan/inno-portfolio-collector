from __future__ import annotations

import re


MAX_DIAGNOSTIC_LENGTH = 4096
_SECRET_RE = re.compile(
    r"(?i)(auth-key|pass_ticket|appmsg_token|token|ticket|uin)=([^&\s\"']+)"
)
_DELIMITED_SECRET_RE = re.compile(
    r"(?i)(?<![\w-])((?:\"|')?(?:auth-key|pass_ticket|appmsg_token|token|ticket|uin)"
    r"(?:\"|')?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^&\s,\"']+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(?<![\w-])((?:\"|')?authorization(?:\"|')?\s*[:=]\s*"
    r"(?:\"|')?bearer\s+(?:\"|')?)[^\s,}\"']+"
)
_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:auth-key|pass_ticket|appmsg_token|token|ticket|uin)\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"
)
_LOCAL_PATH_START = re.compile(
    r"(?i)(?:file://)?/(?:Users|Volumes|private|var|tmp|home|opt)(?:/|$)|"
    r"(?<![\w])[A-Z]:\\(?:Users|Volumes|Temp)(?:\\|$)"
)


def sanitize_diagnostic(value: object, fallback: str = "operation failed") -> str:
    try:
        message = str(value)
    except Exception:
        return fallback
    if not message:
        return fallback

    sanitized = _DELIMITED_SECRET_RE.sub(r"\1[REDACTED]", message)
    sanitized = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _CLI_SECRET_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_RE.sub(r"\1=[REDACTED]", sanitized)

    path = _LOCAL_PATH_START.search(sanitized)
    if path is not None:
        prefix = sanitized[: path.start()].rstrip(" \t\r\n'\"([{:=")
        sanitized = f"{prefix} [path]".strip()
    return sanitized[:MAX_DIAGNOSTIC_LENGTH] or fallback
