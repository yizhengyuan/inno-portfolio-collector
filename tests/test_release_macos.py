from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import release_macos


class MacReleaseTests(unittest.TestCase):
    def fake_apps(self, root: Path) -> Path:
        apps = root / "apps"
        for role, executable, bundle_id, plugins in (
            (
                "Collector", "InnoCollectorApp", "com.inno.news.collector",
                ("InnoCollectorHelper", "MooreExporterHelper"),
            ),
            ("Reader", "InnoReaderApp", "com.inno.news.reader", ("InnoReaderHelper",)),
        ):
            app = apps / f"Inno{role}.app"
            binary = app / f"Contents/MacOS/{executable}"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"swift")
            for name in plugins:
                path = app / f"Contents/PlugIns/{name}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"adhoc")
            plist = {
                "CFBundleIdentifier": bundle_id,
                "CFBundleVersion": "1",
                "CFBundleShortVersionString": "0.1.0",
            }
            info = app / "Contents/Info.plist"
            info.write_bytes(plistlib.dumps(plist))
        return apps

    def test_release_refuses_missing_or_invalid_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = self.fake_apps(root)
            for identity in (None, "", "Apple Development: Example"):
                environment = {} if identity is None else {"MACOS_SIGNING_IDENTITY": identity}
                with self.subTest(identity=identity):
                    with self.assertRaisesRegex(
                        release_macos.ReleaseError,
                        "MACOS_SIGNING_IDENTITY is required",
                    ):
                        release_macos.release(
                            apps=apps,
                            output=root / "release",
                            version="0.1.0",
                            notarize=False,
                            environment=environment,
                        )

    def test_notarization_requires_all_apple_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = self.fake_apps(root)
            base = {"MACOS_SIGNING_IDENTITY": "Developer ID Application: Example (TEAM123)"}
            for missing in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"):
                environment = {
                    **base,
                    "APPLE_ID": "person@example.com",
                    "APPLE_TEAM_ID": "TEAM123",
                    "APPLE_APP_PASSWORD": "app-password",
                }
                environment.pop(missing)
                with self.subTest(missing=missing):
                    with self.assertRaisesRegex(release_macos.ReleaseError, f"{missing} is required"):
                        release_macos.release(
                            apps=apps,
                            output=root / "release",
                            version="0.1.0",
                            notarize=True,
                            environment=environment,
                        )

    def test_signs_in_order_creates_two_dmgs_notarizes_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = self.fake_apps(root)
            output = root / "release"
            events: list[str] = []
            commands: list[list[str]] = []

            def signed_helpers(identity: str, destination: Path, runner):
                events.append("helpers-signed")
                result = {}
                for role, name in (
                    ("collector", "InnoCollectorHelper"),
                    ("reader", "InnoReaderHelper"),
                    ("moore", "MooreExporterHelper"),
                ):
                    path = destination / role / name
                    path.parent.mkdir(parents=True)
                    path.write_bytes((identity + name).encode())
                    result[role] = path
                return result

            def run(command, **kwargs):
                command = [str(value) for value in command]
                commands.append(command)
                if command[0] == "codesign" and "--sign" in command:
                    events.append("outer-signed" if command[-1].endswith(".app") else "swift-signed")
                if command[0] == "hdiutil":
                    Path(command[-1]).write_bytes(("dmg:" + command[-1]).encode())
                if command[:3] == ["xcrun", "notarytool", "submit"]:
                    return subprocess.CompletedProcess(command, 0, json.dumps({"status": "Accepted"}), "")
                return subprocess.CompletedProcess(command, 0, "", "")

            manifest_path = release_macos.release(
                apps=apps,
                output=output,
                version="0.1.0",
                notarize=True,
                environment={
                    "MACOS_SIGNING_IDENTITY": "Developer ID Application: Example (TEAM123)",
                    "APPLE_ID": "person@example.com",
                    "APPLE_TEAM_ID": "TEAM123",
                    "APPLE_APP_PASSWORD": "app-password",
                },
                runner=run,
                helper_builder=signed_helpers,
            )

            self.assertLess(events.index("helpers-signed"), events.index("swift-signed"))
            self.assertLess(events.index("swift-signed"), events.index("outer-signed"))
            dmgs = sorted(output.glob("*.dmg"))
            self.assertEqual(len(dmgs), 2)
            submissions = [command for command in commands if command[:3] == ["xcrun", "notarytool", "submit"]]
            self.assertEqual(len(submissions), 2)
            self.assertTrue(all("--wait" in command for command in submissions))
            self.assertEqual(
                sum(command[:3] == ["xcrun", "stapler", "staple"] for command in commands),
                2,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "0.1.0")
            self.assertTrue(manifest["notarized"])
            self.assertEqual({item["bundle_id"] for item in manifest["artifacts"]}, {
                "com.inno.news.collector", "com.inno.news.reader",
            })
            for item in manifest["artifacts"]:
                path = output / item["dmg"]
                self.assertEqual(item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
