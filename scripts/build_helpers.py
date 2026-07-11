from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[..., subprocess.CompletedProcess[str]]
READER_FORBIDDEN_MARKERS = (
    "wechat_exporter",
    "wechat_downloader",
    "mooreexporteradapter",
    "collector_helper",
    "auth-key",
    ".moore",
)


class HelperBuildError(RuntimeError):
    pass


def _default_moore_source() -> Path:
    for ancestor in ROOT.parents:
        candidate = ancestor / "moore-wechat-article-downloader/scripts"
        if candidate.is_dir():
            return candidate
    return ROOT.parent / "moore-wechat-article-downloader/scripts"


def pyinstaller_commands(output: Path, moore_source: Path) -> list[list[str]]:
    output = Path(output)
    moore_source = Path(moore_source)
    for name in ("wechat_exporter.py", "wechat_downloader.py"):
        if not (moore_source / name).is_file():
            raise HelperBuildError(f"Moore source is missing {name}")

    entries = (
        ("collector", "InnoCollectorHelper", ROOT / "packaging/collector_helper_entry.py", None),
        ("reader", "InnoReaderHelper", ROOT / "packaging/reader_helper_entry.py", None),
        ("moore", "MooreExporterHelper", ROOT / "packaging/moore_exporter_entry.py", moore_source),
    )
    commands: list[list[str]] = []
    for role, name, entry, search_path in entries:
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
        if search_path is not None:
            command.extend(["--paths", str(search_path)])
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


def _report(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{path.name} size={path.stat().st_size} sha256={digest}")


def build(
    *,
    output: Path,
    moore_source: Path,
    clean: bool,
    runner: Runner = subprocess.run,
) -> dict[str, Path]:
    output = Path(output)
    if clean and output.exists():
        shutil.rmtree(output)
    commands = pyinstaller_commands(output, moore_source)
    for command in commands:
        _run(command, runner=runner, text=True, capture_output=True, timeout=900)

    binaries = {
        "collector": output / "collector/InnoCollectorHelper",
        "reader": output / "reader/InnoReaderHelper",
        "moore": output / "moore/MooreExporterHelper",
    }
    missing = [path.name for path in binaries.values() if not path.is_file()]
    if missing:
        raise HelperBuildError("missing helper output: " + ", ".join(missing))
    _smoke_role(binaries["collector"], "collector", runner=runner)
    _smoke_role(binaries["reader"], "reader", runner=runner)
    _run(
        [str(binaries["moore"]), "--help"],
        runner=runner,
        text=True,
        capture_output=True,
        timeout=30,
    )
    audit_reader_binary(binaries["reader"], runner=runner)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        build(
            output=arguments.output,
            moore_source=arguments.moore_source,
            clean=arguments.clean,
        )
    except HelperBuildError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
