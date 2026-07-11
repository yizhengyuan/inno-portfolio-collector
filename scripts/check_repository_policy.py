from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
MAX_CONTENT_SCAN_BYTES = 2_000_000

REQUIRED_MARKERS = {
    "LICENSE": (b"MIT License", b"Permission is hereby granted"),
    "SECURITY.md": ("私密漏洞报告".encode("utf-8"),),
    "THIRD_PARTY_NOTICES.md": (
        b"wechat-article-exporter",
        b"moore-wechat-article-downloader",
    ),
    "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt": (
        b"MIT License",
    ),
    "third_party/licenses/wechat-article-exporter-LICENSE.txt": (b"MIT License",),
}

USER_MATERIAL_PREFIXES = (
    ".superpowers/",
    "英诺项目清单-2026/",
    "runtime/",
    ".moore/",
)
CREDENTIAL_EXTENSIONS = {".pem", ".key", ".p12", ".pfx"}
CREDENTIAL_NAMES = {"credential", "credentials", "secret", "secrets"}
CONTENT_RULES = (
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "github-token",
        re.compile(rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{50,})"),
    ),
    ("aws-access-key", re.compile(rb"AKIA[A-Z0-9]{16}")),
)


@dataclass(frozen=True, order=True)
class PolicyViolation:
    path: str
    rule: str


def _is_credential_path(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519"}
        or name in CREDENTIAL_NAMES
        or PurePosixPath(name).suffix in CREDENTIAL_EXTENSIONS
    )


def _read_tracked_bytes(path: str) -> bytes:
    candidate = ROOT / path
    if candidate.is_symlink():
        return os.fsencode(os.readlink(candidate))
    return candidate.read_bytes()


def audit_repository(
    tracked_paths: Iterable[str],
    read_bytes: Callable[[str], bytes],
) -> list[PolicyViolation]:
    paths = set(tracked_paths)
    violations: set[PolicyViolation] = set()

    for required_path in REQUIRED_MARKERS:
        if required_path not in paths:
            violations.add(
                PolicyViolation(required_path, "required-file-missing")
            )

    for path in sorted(paths):
        if path.startswith(USER_MATERIAL_PREFIXES):
            violations.add(PolicyViolation(path, "user-material"))
        if _is_credential_path(path):
            violations.add(PolicyViolation(path, "credential-file"))

        try:
            content = read_bytes(path)
        except OSError:
            violations.add(PolicyViolation(path, "tracked-file-unreadable"))
            continue

        required_markers = REQUIRED_MARKERS.get(path)
        if required_markers and any(marker not in content for marker in required_markers):
            violations.add(PolicyViolation(path, "required-marker-missing"))

        if len(content) > MAX_CONTENT_SCAN_BYTES or b"\x00" in content:
            continue
        for rule, pattern in CONTENT_RULES:
            if pattern.search(content) is not None:
                violations.add(PolicyViolation(path, rule))

    return sorted(violations)


def tracked_paths(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        path.decode("utf-8")
        for path in result.stdout.split(b"\x00")
        if path
    ]


def main() -> int:
    paths = tracked_paths()
    violations = audit_repository(paths, _read_tracked_bytes)
    if violations:
        for violation in violations:
            print(f"{violation.path}: {violation.rule}")
        return 1
    print(f"repository policy passed: {len(paths)} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
