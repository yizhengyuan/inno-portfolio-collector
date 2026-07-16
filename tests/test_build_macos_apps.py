from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import build_macos_apps


ROOT = Path(__file__).resolve().parents[1]


class MacAppBundleTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        swift = root / "swift"
        helpers = root / "helpers"
        swift.mkdir()
        for name in ("InnoCollectorApp", "InnoReaderApp"):
            (swift / name).write_bytes((name + " safe").encode())
        for role, name in (
            ("collector-web", "InnoCollectorWebServer"),
            ("reader", "InnoReaderHelper"),
        ):
            path = helpers / role / name
            path.parent.mkdir(parents=True)
            path.write_bytes((name + " safe").encode())
        return swift, helpers

    def test_exact_role_isolated_layout_and_byte_preserved_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            swift, helpers = self.fixture(root)
            output = root / "apps"
            commands: list[list[str]] = []

            def run(command, **kwargs):
                commands.append([str(value) for value in command])
                return subprocess.CompletedProcess(command, 0, "", "")

            apps = build_macos_apps.assemble_apps(
                swift_bin=swift,
                helpers=helpers,
                output=output,
                runner=run,
            )

            collector = apps["collector"]
            reader = apps["reader"]
            expected_collector = {
                "Contents/MacOS/InnoCollectorApp",
                "Contents/PlugIns/InnoCollectorWebServer",
                "Contents/Resources/config/projects.json",
                "Contents/Resources/ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/inno-news-suite-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/THIRD_PARTY_NOTICES.md",
                "Contents/Info.plist",
            }
            expected_reader = {
                "Contents/MacOS/InnoReaderApp",
                "Contents/PlugIns/InnoReaderHelper",
                "Contents/Resources/ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/inno-news-suite-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/THIRD_PARTY_NOTICES.md",
                "Contents/Info.plist",
            }
            self.assertEqual(self.files(collector), expected_collector)
            self.assertEqual(self.files(reader), expected_reader)
            web_server = collector / "Contents/PlugIns/InnoCollectorWebServer"
            self.assertTrue(web_server.is_file())
            self.assertFalse(web_server.is_symlink())
            self.assertEqual(web_server.stat().st_mode & 0o777, 0o755)
            self.assertEqual(
                (collector / "Contents/Resources/config/projects.json").read_bytes(),
                (ROOT / "config/projects.json").read_bytes(),
            )
            for app in (collector, reader):
                self.assertEqual(
                    (
                        app
                        / "Contents/Resources/ThirdPartyLicenses/inno-news-suite-LICENSE.txt"
                    ).read_bytes(),
                    (ROOT / "LICENSE").read_bytes(),
                )
            reader_files = {value.casefold() for value in self.files(reader)}
            self.assertFalse(any("collector" in value for value in reader_files))
            self.assertFalse(any("mooreexporter" in value for value in reader_files))
            self.assertFalse(any(value.endswith("projects.json") for value in reader_files))
            self.assertEqual(
                sum(command[0] == "codesign" and "--verify" in command for command in commands),
                4,
            )
            self.assertEqual(
                {
                    Path(command[-1]).name
                    for command in commands
                    if command[0] == "codesign"
                    and "--verify" in command
                    and "/PlugIns/" in command[-1]
                },
                {
                    "InnoCollectorWebServer",
                    "InnoReaderHelper",
                },
            )
            helper_runtime_resigns = [
                command
                for command in commands
                if command[0] == "codesign"
                and "--sign" in command
                and "/PlugIns/" in command[-1]
            ]
            self.assertEqual(helper_runtime_resigns, [])
            strip_commands = [command for command in commands if command[0] == "strip"]
            self.assertEqual(len(strip_commands), 2)
            self.assertTrue(
                all(command[1:3] == ["-S", "-x"] for command in strip_commands)
            )
            self.assertEqual(
                {Path(command[-1]).name for command in strip_commands},
                {"InnoCollectorApp", "InnoReaderApp"},
            )
            for strip_command in strip_commands:
                target = strip_command[-1]
                sign_command = next(
                    command
                    for command in commands
                    if command[0] == "codesign"
                    and "--sign" in command
                    and command[-1] == target
                )
                self.assertLess(
                    commands.index(strip_command), commands.index(sign_command)
                )

    def test_plists_and_entitlements_are_least_privilege(self) -> None:
        collector = plistlib.loads((ROOT / "packaging/Info-Collector.plist").read_bytes())
        reader = plistlib.loads((ROOT / "packaging/Info-Reader.plist").read_bytes())
        collector_entitlements = plistlib.loads(
            (ROOT / "packaging/collector.entitlements").read_bytes()
        )
        reader_entitlements = plistlib.loads(
            (ROOT / "packaging/reader.entitlements").read_bytes()
        )

        self.assertEqual(collector["CFBundleIdentifier"], "com.inno.news.collector")
        self.assertEqual(reader["CFBundleIdentifier"], "com.inno.news.reader")
        self.assertEqual(collector["LSMinimumSystemVersion"], "13.0")
        self.assertEqual(reader["LSMinimumSystemVersion"], "13.0")
        self.assertEqual(self.extensions(collector), {"inno-drafts"})
        self.assertEqual(self.extensions(reader), {"inno-update", "zip"})
        self.assertEqual(collector_entitlements, {"com.apple.security.network.client": True})
        self.assertEqual(reader_entitlements, {})

    def test_bundle_rejects_local_absolute_paths_left_after_stripping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            swift, helpers = self.fixture(root)
            (swift / "InnoReaderApp").write_bytes(
                b"release binary /Users/alice/private/source.swift"
            )

            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaisesRegex(
                build_macos_apps.AppBuildError,
                "local absolute path",
            ):
                build_macos_apps.assemble_apps(
                    swift_bin=swift,
                    helpers=helpers,
                    output=root / "apps",
                    runner=run,
                )

    def test_bundle_rejects_volume_path_in_resource_without_disclosing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_path = b"/Volumes/PrivateDisk/confidential/build.log"
            resource = root / "Contents/Resources/notice.txt"
            resource.parent.mkdir(parents=True)
            resource.write_bytes(b"safe prefix\n" + private_path)

            with self.assertRaises(build_macos_apps.AppBuildError) as caught:
                build_macos_apps._audit_local_paths(root)

            self.assertIn("local absolute path", str(caught.exception))
            self.assertNotIn(private_path.decode(), str(caught.exception))

    def test_unsafe_web_server_is_rejected_and_both_apps_are_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            swift, helpers = self.fixture(root)
            web_server = helpers / "collector-web/InnoCollectorWebServer"
            web_server.write_bytes(b"binary /Users/alice/private/source.py")
            output = root / "apps"

            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaisesRegex(
                build_macos_apps.AppBuildError,
                "local absolute path",
            ):
                build_macos_apps.assemble_apps(
                    swift_bin=swift,
                    helpers=helpers,
                    output=output,
                    runner=run,
                )

            self.assertFalse(output.exists())
            self.assertFalse((root / "apps/InnoCollector.app").exists())
            self.assertFalse((root / "apps/InnoReader.app").exists())
            self.assertEqual(list(root.glob(".inno-apps-*")), [])

    @staticmethod
    def files(root: Path) -> set[str]:
        return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}

    @staticmethod
    def extensions(plist: dict) -> set[str]:
        return {
            extension
            for document in plist["CFBundleDocumentTypes"]
            for extension in document["CFBundleTypeExtensions"]
        }


if __name__ == "__main__":
    unittest.main()
