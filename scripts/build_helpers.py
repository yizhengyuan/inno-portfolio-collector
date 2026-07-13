from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[..., subprocess.CompletedProcess[str]]
READER_FORBIDDEN_MARKERS = (
    "innocollectorwebserver",
    "collector_web_server",
    "inno_collector.web",
    "wechat_exporter",
    "wechat_downloader",
    "mooreexporteradapter",
    "mooreexporterhelper",
    "moore_runtime",
    "collector_helper",
    "auth-key",
    ".moore",
    "projects.json",
)
WEB_ARCHIVE_REQUIRED = (
    "wechat_exporter",
    "wechat_downloader",
    "inno_collector/web/assets/index.html",
    "inno_collector/web/assets/app.css",
    "inno_collector/web/assets/app.js",
    "inno_collector/web/resources/projects.json",
    "ThirdPartyLicenses/LICENSE",
    "ThirdPartyLicenses/NOTICE.md",
    "ThirdPartyLicenses/THIRD_PARTY_NOTICES.md",
    "ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt",
    "ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt",
)
_LOCAL_BUILD_PATH = re.compile(r"/(?:Users|Volumes)/[^/\x00]+/")
_HIGH_CONFIDENCE_SECRETS = (
    re.compile(r"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{50,})"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(
        r"(?i)\b(?:auth[-_]?key|cookie|token|password|secret)\b"
        r"[\"']?\s*[:=]\s*[\"'][A-Za-z0-9+/=_-]{24,}[\"']"
    ),
)


class HelperBuildError(RuntimeError):
    pass


def _default_moore_source() -> Path:
    for ancestor in ROOT.parents:
        candidate = ancestor / "moore-wechat-article-downloader/scripts"
        if candidate.is_dir():
            return candidate
    return ROOT.parent / "moore-wechat-article-downloader/scripts"


def pyinstaller_commands(
    output: Path,
    moore_source: Path,
    codesign_identity: str | None = None,
) -> list[list[str]]:
    output = Path(output)
    moore_source = Path(moore_source)
    for name in ("wechat_exporter.py", "wechat_downloader.py"):
        if not (moore_source / name).is_file():
            raise HelperBuildError(f"Moore source is missing {name}")

    web_assets = ROOT / "src/inno_collector/web/assets"
    web_resources = ROOT / "src/inno_collector/web/resources"
    third_party_licenses = ROOT / "third_party/licenses"
    required_inputs = (
        web_assets / "index.html",
        web_assets / "app.css",
        web_assets / "app.js",
        web_resources / "projects.json",
        ROOT / "LICENSE",
        ROOT / "NOTICE.md",
        ROOT / "THIRD_PARTY_NOTICES.md",
        third_party_licenses / "wechat-article-exporter-LICENSE.txt",
        third_party_licenses / "moore-wechat-article-downloader-LICENSE.txt",
    )
    missing_inputs = [path.name for path in required_inputs if not path.is_file()]
    if missing_inputs:
        raise HelperBuildError("missing Web build input: " + ", ".join(missing_inputs))

    source_root = ROOT / "src"
    entries = (
        (
            "reader",
            "InnoReaderHelper",
            ROOT / "packaging/reader_helper_entry.py",
            (source_root,),
            (),
            (),
        ),
        (
            "collector-web",
            "InnoCollectorWebServer",
            ROOT / "packaging/collector_web_server_entry.py",
            (source_root, moore_source),
            (
                (web_assets, "inno_collector/web/assets"),
                (web_resources, "inno_collector/web/resources"),
                (third_party_licenses, "ThirdPartyLicenses"),
                (ROOT / "LICENSE", "ThirdPartyLicenses"),
                (ROOT / "NOTICE.md", "ThirdPartyLicenses"),
                (ROOT / "THIRD_PARTY_NOTICES.md", "ThirdPartyLicenses"),
            ),
            ("wechat_exporter", "wechat_downloader"),
        ),
    )
    commands: list[list[str]] = []
    for role, name, entry, search_paths, data_files, hidden_imports in entries:
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            name,
            "--distpath",
            str(output / role),
            "--workpath",
            str(output / "work" / role),
            "--specpath",
            str(output / "spec" / role),
        ]
        for search_path in search_paths:
            command.extend(["--paths", str(search_path)])
        for source, destination in data_files:
            command.extend(["--add-data", f"{source}:{destination}"])
        for hidden_import in hidden_imports:
            command.extend(["--hidden-import", hidden_import])
        if codesign_identity is not None:
            command.extend(["--codesign-identity", codesign_identity])
        command.append(str(entry))
        commands.append(command)
    return commands


def audit_reader_binary(reader: Path, *, runner: Runner = subprocess.run) -> None:
    result = runner(
        ["strings", str(reader)],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise HelperBuildError("unable to audit reader helper")
    lowered = result.stdout.casefold()
    hits = [marker for marker in READER_FORBIDDEN_MARKERS if marker in lowered]
    if hits:
        raise HelperBuildError("reader helper contains collector-only markers")


def audit_collector_web_binary(
    web_server: Path,
    *,
    runner: Runner = subprocess.run,
) -> None:
    strings_result = runner(
        ["strings", str(web_server)],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if strings_result.returncode != 0 or not isinstance(strings_result.stdout, str):
        raise HelperBuildError("unable to audit collector Web server")
    content = strings_result.stdout
    if _LOCAL_BUILD_PATH.search(content) or any(
        pattern.search(content) for pattern in _HIGH_CONFIDENCE_SECRETS
    ):
        raise HelperBuildError("collector Web server contains unsafe build material")

    archive_result = runner(
        [
            sys.executable,
            "-m",
            "PyInstaller.utils.cliutils.archive_viewer",
            "--list",
            "--recursive",
            "--brief",
            str(web_server),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if archive_result.returncode != 0 or not isinstance(archive_result.stdout, str):
        raise HelperBuildError("unable to inspect collector Web server archive")
    listing = archive_result.stdout.replace("\\", "/")
    if any(required not in listing for required in WEB_ARCHIVE_REQUIRED):
        raise HelperBuildError("collector Web server archive is missing required resources")


def _run(command: Sequence[str], *, runner: Runner, **kwargs) -> subprocess.CompletedProcess[str]:
    result = runner(list(command), check=False, **kwargs)
    if result.returncode != 0:
        raise HelperBuildError(f"command failed: {Path(command[0]).name}")
    return result


def _smoke_role(path: Path, role: str, *, runner: Runner) -> None:
    request = {"id": "build-smoke", "command": "status", "arguments": {}}
    result = _run(
        [str(path)],
        runner=runner,
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=30,
    )
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise HelperBuildError(f"{role} helper returned invalid smoke output") from None
    if (
        not isinstance(response, dict)
        or response.get("id") != "build-smoke"
        or response.get("ok") is not True
        or not isinstance(response.get("result"), dict)
        or response["result"].get("role") != role
    ):
        raise HelperBuildError(f"{role} helper failed role smoke")


def _smoke_web_server(path: Path, *, runner: Runner) -> None:
    result = _run(
        [str(path), "--smoke"],
        runner=runner,
        text=True,
        capture_output=True,
        timeout=30,
    )
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise HelperBuildError("collector Web server returned invalid smoke output") from None
    if (
        not isinstance(response, dict)
        or set(response) != {"role", "protocol"}
        or response.get("role") != "collector-web"
        or response.get("protocol") != 1
    ):
        raise HelperBuildError("collector Web server failed role smoke")


def _report(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{path.name} size={path.stat().st_size} sha256={digest}")


def build(
    *,
    output: Path,
    moore_source: Path,
    clean: bool,
    codesign_identity: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Path]:
    output = Path(output)
    if clean and output.exists():
        shutil.rmtree(output)
    commands = pyinstaller_commands(output, moore_source, codesign_identity)
    for command in commands:
        _run(command, runner=runner, text=True, capture_output=True, timeout=900)

    binaries = {
        "collector-web": output / "collector-web/InnoCollectorWebServer",
        "reader": output / "reader/InnoReaderHelper",
    }
    missing = [path.name for path in binaries.values() if not path.is_file()]
    if missing:
        raise HelperBuildError("missing helper output: " + ", ".join(missing))
    _smoke_role(binaries["reader"], "reader", runner=runner)
    _smoke_web_server(binaries["collector-web"], runner=runner)
    audit_reader_binary(binaries["reader"], runner=runner)
    audit_collector_web_binary(binaries["collector-web"], runner=runner)
    for path in binaries.values():
        _report(path)
    return binaries


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build isolated macOS helper binaries")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--moore-source",
        type=Path,
        default=_default_moore_source(),
    )
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--codesign-identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        build(
            output=arguments.output,
            moore_source=arguments.moore_source,
            clean=arguments.clean,
            codesign_identity=arguments.codesign_identity,
        )
    except HelperBuildError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
