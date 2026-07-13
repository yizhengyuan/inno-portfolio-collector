from __future__ import annotations

import argparse
import hashlib
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
NOTICE_SOURCE = ROOT / "packaging/collector_web_pilot_README_zh_CN.txt"
NOTICE_NAME = "安装说明.txt"
STAGED_APP_NAME = "英诺资讯采集.app"
EXPECTED_PLUGINS = {"InnoCollectorWebServer"}
REQUIRED_APP_FILES = (
    "Contents/MacOS/InnoCollectorApp",
    "Contents/PlugIns/InnoCollectorWebServer",
    "Contents/Resources/config/projects.json",
    "Contents/Resources/ThirdPartyLicenses/inno-news-suite-LICENSE.txt",
    "Contents/Resources/ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt",
    "Contents/Resources/ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt",
    "Contents/Resources/ThirdPartyLicenses/THIRD_PARTY_NOTICES.md",
)
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){2}\Z")
Runner = Callable[..., subprocess.CompletedProcess[str]]


class PilotDmgError(RuntimeError):
    pass


def _run(
    command: Sequence[str],
    *,
    runner: Runner,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    result = runner(list(command), check=False, **kwargs)
    if result.returncode != 0:
        raise PilotDmgError(f"command failed: {Path(command[0]).name}")
    return result


def _app_info(app: Path) -> dict[str, object]:
    app = Path(app)
    if app.name != "InnoCollector.app" or not app.is_dir() or app.is_symlink():
        raise PilotDmgError("invalid Collector app input")
    if any(path.is_symlink() for path in app.rglob("*")):
        raise PilotDmgError("invalid Collector app input")

    macos = app / "Contents/MacOS"
    plugins = app / "Contents/PlugIns"
    if (
        not macos.is_dir()
        or {path.name for path in macos.iterdir()} != {"InnoCollectorApp"}
        or not plugins.is_dir()
        or {path.name for path in plugins.iterdir()} != EXPECTED_PLUGINS
    ):
        raise PilotDmgError("invalid Collector helper layout")
    if any(not (app / relative).is_file() for relative in REQUIRED_APP_FILES):
        raise PilotDmgError("invalid Collector app layout")
    for relative in (
        "Contents/MacOS/InnoCollectorApp",
        "Contents/PlugIns/InnoCollectorWebServer",
    ):
        executable = app / relative
        if executable.is_symlink() or executable.stat().st_mode & 0o111 == 0:
            raise PilotDmgError("invalid Collector executable layout")

    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        raise PilotDmgError("invalid Collector Info.plist") from None
    if not isinstance(info, dict):
        raise PilotDmgError("invalid Collector Info.plist")
    if (
        info.get("CFBundleIdentifier") != "com.inno.news.collector"
        or info.get("CFBundleExecutable") != "InnoCollectorApp"
    ):
        raise PilotDmgError("invalid Collector Info.plist")
    return info


def _app_version(app: Path) -> str:
    version = _app_info(app).get("CFBundleShortVersionString")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise PilotDmgError("invalid Collector app version")
    return version


def pilot_dmg_name(app: Path, build_date: date) -> str:
    if not isinstance(build_date, date):
        raise PilotDmgError("invalid pilot build date")
    version = _app_version(Path(app))
    return f"InnoCollector-Web-{version}-pilot-{build_date:%Y%m%d}.dmg"


def stage_pilot_contents(app: Path, destination: Path) -> Path:
    app = Path(app)
    destination = Path(destination)
    _app_info(app)
    if destination.exists() or destination.is_symlink():
        raise PilotDmgError("pilot staging destination already exists")
    if not NOTICE_SOURCE.is_file() or NOTICE_SOURCE.is_symlink():
        raise PilotDmgError("pilot installation notice is missing")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        shutil.copytree(app, destination / STAGED_APP_NAME, symlinks=False)
        (destination / "Applications").symlink_to(
            "/Applications",
            target_is_directory=True,
        )
        shutil.copyfile(NOTICE_SOURCE, destination / NOTICE_NAME, follow_symlinks=False)
        if {path.name for path in destination.iterdir()} != {
            STAGED_APP_NAME,
            "Applications",
            NOTICE_NAME,
        }:
            raise PilotDmgError("pilot staging layout is incomplete")
        return destination
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def build_pilot_dmg(
    *,
    app: Path,
    output: Path,
    build_date: date | None = None,
    runner: Runner = subprocess.run,
) -> Path:
    app = Path(app)
    output = Path(output)
    selected_date = date.today() if build_date is None else build_date
    dmg_name = pilot_dmg_name(app, selected_date)
    if output.exists() and not output.is_dir():
        raise PilotDmgError("pilot output is not a directory")
    output.mkdir(parents=True, exist_ok=True)
    dmg = output / dmg_name
    if dmg.exists() or dmg.is_symlink():
        raise PilotDmgError("pilot DMG output already exists")

    _run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        runner=runner,
        text=True,
        capture_output=True,
        timeout=120,
    )

    stage = Path(tempfile.mkdtemp(prefix=".inno-web-pilot-", dir=output.parent))
    try:
        contents = stage_pilot_contents(app, stage / "contents")
        _run(
            [
                "hdiutil",
                "create",
                "-volname",
                f"InnoCollector Web {_app_version(app)} Pilot",
                "-srcfolder",
                str(contents),
                "-format",
                "UDZO",
                str(dmg),
            ],
            runner=runner,
            text=True,
            capture_output=True,
            timeout=900,
        )
        if not dmg.is_file() or dmg.is_symlink():
            raise PilotDmgError("hdiutil did not create the expected pilot DMG")
        return dmg
    except BaseException:
        dmg.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one local-only InnoCollector Web pilot DMG"
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        dmg = build_pilot_dmg(app=arguments.app, output=arguments.output)
    except PilotDmgError as error:
        print(str(error), file=sys.stderr)
        return 2
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    print(f"{dmg} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
