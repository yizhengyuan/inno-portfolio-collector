from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

try:
    from scripts import build_helpers
except ImportError:
    import build_helpers


ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[..., subprocess.CompletedProcess[str]]
HelperBuilder = Callable[[str, Path, Runner], dict[str, Path]]


class ReleaseError(RuntimeError):
    pass


def _run(
    command: Sequence[str],
    *,
    runner: Runner,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    result = runner(list(command), check=False, **kwargs)
    if result.returncode != 0:
        raise ReleaseError(f"command failed: {Path(command[0]).name}")
    return result


def _default_helper_builder(
    identity: str,
    destination: Path,
    runner: Runner,
) -> dict[str, Path]:
    try:
        return build_helpers.build(
            output=destination,
            moore_source=build_helpers._default_moore_source(),
            clean=True,
            codesign_identity=identity,
            runner=runner,
        )
    except build_helpers.HelperBuildError as error:
        raise ReleaseError(str(error)) from None


def _validated_environment(
    environment: Mapping[str, str],
    *,
    notarize: bool,
) -> tuple[str, dict[str, str]]:
    identity = environment.get("MACOS_SIGNING_IDENTITY", "").strip()
    if not identity.startswith("Developer ID Application:"):
        raise ReleaseError("MACOS_SIGNING_IDENTITY is required and must be a Developer ID Application identity")
    apple: dict[str, str] = {}
    if notarize:
        for name in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"):
            value = environment.get(name, "").strip()
            if not value:
                raise ReleaseError(f"{name} is required for notarization")
            apple[name] = value
    return identity, apple


def _copy_app(source: Path, destination: Path) -> None:
    if not source.is_dir() or any(path.is_symlink() for path in source.rglob("*")):
        raise ReleaseError(f"invalid app input: {source.name}")
    shutil.copytree(source, destination, symlinks=False)


def _replace_helpers(apps: dict[str, Path], helpers: dict[str, Path]) -> None:
    rows = (
        (apps["collector"], "InnoCollectorHelper", helpers["collector"]),
        (apps["collector"], "MooreExporterHelper", helpers["moore"]),
        (apps["reader"], "InnoReaderHelper", helpers["reader"]),
    )
    for app, name, source in rows:
        destination = app / f"Contents/PlugIns/{name}"
        if not source.is_file() or not destination.is_file():
            raise ReleaseError("signed helper layout is incomplete")
        shutil.copyfile(source, destination, follow_symlinks=False)
        destination.chmod(0o755)


def _sign_and_verify(
    role: str,
    app: Path,
    identity: str,
    *,
    runner: Runner,
) -> None:
    for helper in sorted((app / "Contents/PlugIns").iterdir()):
        _run(
            ["codesign", "--verify", "--strict", "--verbose=2", str(helper)],
            runner=runner,
            text=True,
            capture_output=True,
        )
    swift_name = "InnoCollectorApp" if role == "collector" else "InnoReaderApp"
    _run(
        [
            "codesign", "--force", "--options", "runtime", "--timestamp",
            "--sign", identity, str(app / f"Contents/MacOS/{swift_name}"),
        ],
        runner=runner,
        text=True,
        capture_output=True,
    )
    _run(
        [
            "codesign", "--force", "--options", "runtime", "--timestamp",
            "--sign", identity,
            "--entitlements", str(ROOT / f"packaging/{role}.entitlements"),
            str(app),
        ],
        runner=runner,
        text=True,
        capture_output=True,
    )
    _run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        runner=runner,
        text=True,
        capture_output=True,
    )
    _run(
        ["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)],
        runner=runner,
        text=True,
        capture_output=True,
    )


def _notarize(
    dmg: Path,
    credentials: Mapping[str, str],
    *,
    runner: Runner,
) -> None:
    result = _run(
        [
            "xcrun", "notarytool", "submit", str(dmg),
            "--apple-id", credentials["APPLE_ID"],
            "--team-id", credentials["APPLE_TEAM_ID"],
            "--password", credentials["APPLE_APP_PASSWORD"],
            "--wait", "--output-format", "json",
        ],
        runner=runner,
        text=True,
        capture_output=True,
        timeout=3600,
    )
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise ReleaseError("notarization returned an invalid response") from None
    if not isinstance(response, dict) or response.get("status") != "Accepted":
        raise ReleaseError("notarization was not accepted")
    _run(
        ["xcrun", "stapler", "staple", str(dmg)],
        runner=runner,
        text=True,
        capture_output=True,
        timeout=300,
    )
    _run(
        [
            "spctl", "--assess", "--type", "open",
            "--context", "context:primary-signature", "--verbose=2", str(dmg),
        ],
        runner=runner,
        text=True,
        capture_output=True,
    )


def release(
    *,
    apps: Path,
    output: Path,
    version: str,
    notarize: bool,
    environment: Mapping[str, str] = os.environ,
    runner: Runner = subprocess.run,
    helper_builder: HelperBuilder = _default_helper_builder,
) -> Path:
    identity, apple = _validated_environment(environment, notarize=notarize)
    apps = Path(apps)
    output = Path(output)
    sources = {
        "collector": apps / "InnoCollector.app",
        "reader": apps / "InnoReader.app",
    }
    if any(not path.is_dir() for path in sources.values()):
        raise ReleaseError("both app bundles are required")
    names = {
        "collector": f"InnoCollector-{version}.dmg",
        "reader": f"InnoReader-{version}.dmg",
    }
    if any((output / name).exists() for name in names.values()) or (
        output / "release-manifest.json"
    ).exists():
        raise ReleaseError("release output already exists")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".inno-release-", dir=output.parent))
    try:
        staged_apps = {
            role: stage / source.name for role, source in sources.items()
        }
        for role, source in sources.items():
            _copy_app(source, staged_apps[role])
        signed_helpers = helper_builder(identity, stage / "helpers", runner)
        _replace_helpers(staged_apps, signed_helpers)
        for role, app in staged_apps.items():
            _sign_and_verify(role, app, identity, runner=runner)

        output.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, object]] = []
        for role, app in staged_apps.items():
            dmg = output / names[role]
            _run(
                [
                    "hdiutil", "create", "-volname", app.stem,
                    "-srcfolder", str(app), "-format", "UDZO", str(dmg),
                ],
                runner=runner,
                text=True,
                capture_output=True,
                timeout=900,
            )
            if not dmg.is_file():
                raise ReleaseError("hdiutil did not create the expected DMG")
            if notarize:
                _notarize(dmg, apple, runner=runner)
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            artifacts.append({
                "role": role,
                "bundle_id": info["CFBundleIdentifier"],
                "build": str(info["CFBundleVersion"]),
                "dmg": dmg.name,
                "size": dmg.stat().st_size,
                "sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
            })
        manifest = {
            "version": version,
            "notarized": notarize,
            "artifacts": artifacts,
        }
        manifest_path = output / "release-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest_path
    except BaseException:
        if output.exists():
            for name in (*names.values(), "release-manifest.json"):
                (output / name).unlink(missing_ok=True)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sign, package, and notarize macOS releases")
    parser.add_argument("--apps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--notarize", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        release(
            apps=arguments.apps,
            output=arguments.output,
            version=arguments.version,
            notarize=arguments.notarize,
        )
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
