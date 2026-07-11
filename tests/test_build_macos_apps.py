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
            ("collector", "InnoCollectorHelper"),
            ("reader", "InnoReaderHelper"),
            ("moore", "MooreExporterHelper"),
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
                "Contents/PlugIns/InnoCollectorHelper",
                "Contents/PlugIns/MooreExporterHelper",
                "Contents/Resources/config/projects.json",
                "Contents/Resources/ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt",
                "Contents/Info.plist",
            }
            expected_reader = {
                "Contents/MacOS/InnoReaderApp",
                "Contents/PlugIns/InnoReaderHelper",
                "Contents/Resources/ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt",
                "Contents/Resources/ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt",
                "Contents/Info.plist",
            }
            self.assertTrue(expected_collector.issubset(self.files(collector)))
            self.assertTrue(expected_reader.issubset(self.files(reader)))
            self.assertEqual(
                (collector / "Contents/Resources/config/projects.json").read_bytes(),
                (ROOT / "config/projects.json").read_bytes(),
            )
            reader_files = {value.casefold() for value in self.files(reader)}
            self.assertFalse(any("collector" in value for value in reader_files))
            self.assertFalse(any("mooreexporter" in value for value in reader_files))
            self.assertFalse(any(value.endswith("projects.json") for value in reader_files))
            self.assertEqual(
                sum(command[0] == "codesign" and "--verify" in command for command in commands),
                5,
            )
            helper_runtime_resigns = [
                command
                for command in commands
                if command[0] == "codesign"
                and "--sign" in command
                and "/PlugIns/" in command[-1]
            ]
            self.assertEqual(helper_runtime_resigns, [])

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
