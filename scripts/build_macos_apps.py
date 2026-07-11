from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

try:
    from scripts.build_helpers import HelperBuildError, audit_reader_binary
except ModuleNotFoundError:
    from build_helpers import HelperBuildError, audit_reader_binary


ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[..., subprocess.CompletedProcess[str]]


class AppBuildError(RuntimeError):
    pass


def _run(
    command: Sequence[str],
    *,
    runner: Runner,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    result = runner(list(command), check=False, **kwargs)
    if result.returncode != 0:
        raise AppBuildError(f"command failed: {Path(command[0]).name}")
    return result


def _copy_regular(source: Path, destination: Path, *, executable: bool = False) -> None:
    try:
        details = source.lstat()
    except OSError:
        raise AppBuildError(f"missing build input: {source.name}") from None
    if not stat.S_ISREG(details.st_mode):
        raise AppBuildError(f"unsafe build input: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(0o755 if executable else 0o644)


def _copy_shared_resources(app: Path) -> None:
    licenses = app / "Contents/Resources/ThirdPartyLicenses"
    for name in (
        "wechat-article-exporter-LICENSE.txt",
        "moore-wechat-article-downloader-LICENSE.txt",
    ):
        _copy_regular(ROOT / "third_party/licenses" / name, licenses / name)
    _copy_regular(ROOT / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")


def _audit_reader_bundle(reader: Path, *, runner: Runner) -> None:
    forbidden_names = {
        "innocollectorhelper",
        "mooreexporterhelper",
        "collector_helper",
        "wechat_exporter.py",
        "wechat_downloader.py",
        "projects.json",
        "cookies.sqlite",
    }
    for path in reader.rglob("*"):
        if path.name.casefold() in forbidden_names:
            raise AppBuildError("reader bundle contains collector-only artifact")
    try:
        audit_reader_binary(
            reader / "Contents/PlugIns/InnoReaderHelper",
            runner=runner,
        )
    except HelperBuildError as error:
        raise AppBuildError(str(error)) from None
    secret_assignment = re.compile(
        rb"(?i)(auth-key|cookie|token)\s*[:=]\s*[^\s,;}]+"
    )
    for path in reader.rglob("*"):
        if path.is_file() and path.suffix.casefold() in {".json", ".plist", ".md", ".txt"}:
            if secret_assignment.search(path.read_bytes()):
                raise AppBuildError("reader bundle contains credential material")


def _sign_app(app: Path, role: str, *, runner: Runner) -> None:
    executable = "InnoCollectorApp" if role == "collector" else "InnoReaderApp"
    swift_executable = app / f"Contents/MacOS/{executable}"
    _run(
        [
            "codesign", "--force", "--options", "runtime", "--sign", "-",
            str(swift_executable),
        ],
        runner=runner,
        text=True,
        capture_output=True,
    )
    # PyInstaller onefile helpers already contain ad-hoc signed embedded
    # libraries. Re-signing only their outer bootloader with hardened runtime
    # breaks library validation, so preserve and verify PyInstaller's signature.
    for path in sorted((app / "Contents/PlugIns").iterdir()):
        _run(
            ["codesign", "--verify", "--strict", str(path)],
            runner=runner,
            text=True,
            capture_output=True,
        )
    entitlements = ROOT / f"packaging/{role}.entitlements"
    _run(
        [
            "codesign", "--force", "--options", "runtime", "--sign", "-",
            "--entitlements", str(entitlements), str(app),
        ],
        runner=runner,
        text=True,
        capture_output=True,
    )
    _run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        runner=runner,
        text=True,
        capture_output=True,
    )


def assemble_apps(
    *,
    swift_bin: Path,
    helpers: Path,
    output: Path,
    runner: Runner = subprocess.run,
) -> dict[str, Path]:
    swift_bin = Path(swift_bin)
    helpers = Path(helpers)
    output = Path(output)
    destinations = {
        "collector": output / "InnoCollector.app",
        "reader": output / "InnoReader.app",
    }
    if any(path.exists() for path in destinations.values()):
        raise AppBuildError("app output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".inno-apps-", dir=output.parent))
    try:
        staged = {
            "collector": stage / "InnoCollector.app",
            "reader": stage / "InnoReader.app",
        }
        layouts = {
            "collector": (
                "InnoCollectorApp",
                ((helpers / "collector/InnoCollectorHelper", "InnoCollectorHelper"),
                 (helpers / "moore/MooreExporterHelper", "MooreExporterHelper")),
                ROOT / "packaging/Info-Collector.plist",
            ),
            "reader": (
                "InnoReaderApp",
                ((helpers / "reader/InnoReaderHelper", "InnoReaderHelper"),),
                ROOT / "packaging/Info-Reader.plist",
            ),
        }
        for role, app in staged.items():
            swift_name, helper_rows, plist = layouts[role]
            _copy_regular(
                swift_bin / swift_name,
                app / f"Contents/MacOS/{swift_name}",
                executable=True,
            )
            for source, name in helper_rows:
                _copy_regular(source, app / f"Contents/PlugIns/{name}", executable=True)
            _copy_regular(plist, app / "Contents/Info.plist")
            try:
                plistlib.loads((app / "Contents/Info.plist").read_bytes())
            except (OSError, plistlib.InvalidFileException):
                raise AppBuildError(f"invalid {role} Info.plist") from None
            _copy_shared_resources(app)
            if role == "collector":
                _copy_regular(
                    ROOT / "config/projects.json",
                    app / "Contents/Resources/config/projects.json",
                )
            _run(
                ["plutil", "-lint", str(app / "Contents/Info.plist")],
                runner=runner,
                text=True,
                capture_output=True,
            )
        _audit_reader_bundle(staged["reader"], runner=runner)
        for role, app in staged.items():
            _sign_app(app, role, runner=runner)

        output.mkdir(parents=True, exist_ok=True)
        for role, source in staged.items():
            os.rename(source, destinations[role])
        return destinations
    except BaseException:
        for destination in destinations.values():
            if destination.exists():
                shutil.rmtree(destination)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble role-isolated macOS applications")
    parser.add_argument("--configuration", choices=("debug", "release"), default="release")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    helpers = arguments.output.parent / "helpers"
    try:
        if not arguments.skip_build:
            _run(
                [
                    "swift", "build", "--package-path", str(ROOT / "macos"),
                    "--configuration", arguments.configuration,
                ],
                runner=subprocess.run,
                text=True,
                capture_output=True,
                timeout=900,
            )
            _run(
                [
                    sys.executable, str(ROOT / "scripts/build_helpers.py"),
                    "--output", str(helpers), "--clean",
                ],
                runner=subprocess.run,
                text=True,
                capture_output=True,
                timeout=1800,
            )
        result = _run(
            [
                "swift", "build", "--package-path", str(ROOT / "macos"),
                "--configuration", arguments.configuration, "--show-bin-path",
            ],
            runner=subprocess.run,
            text=True,
            capture_output=True,
            timeout=60,
        )
        swift_bin = Path(result.stdout.strip())
        assemble_apps(swift_bin=swift_bin, helpers=helpers, output=arguments.output)
    except AppBuildError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
