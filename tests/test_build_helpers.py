from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import build_helpers


ROOT = Path(__file__).resolve().parents[1]
WEB_ARCHIVE_CONTENTS = "\n".join(
    (
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
)


class BuildHelperTests(unittest.TestCase):
    def test_four_independent_pyinstaller_commands_and_smokes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "helpers"
            moore = Path(temporary) / "moore"
            moore.mkdir()
            for name in ("wechat_exporter.py", "wechat_downloader.py"):
                (moore / name).write_text("", encoding="utf-8")
            binaries = {
                "InnoCollectorHelper": output / "collector/InnoCollectorHelper",
                "InnoCollectorWebServer": output / "collector-web/InnoCollectorWebServer",
                "InnoReaderHelper": output / "reader/InnoReaderHelper",
                "MooreExporterHelper": output / "moore/MooreExporterHelper",
            }
            for path in binaries.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")

            calls: list[list[str]] = []

            def run(command, **kwargs):
                calls.append([str(value) for value in command])
                executable = Path(command[0]).name
                if executable == "InnoCollectorHelper":
                    request = json.loads(kwargs["input"])
                    return subprocess.CompletedProcess(
                        command, 0, json.dumps({"id": request["id"], "ok": True, "result": {"role": "collector"}}), ""
                    )
                if executable == "InnoReaderHelper":
                    request = json.loads(kwargs["input"])
                    return subprocess.CompletedProcess(
                        command, 0, json.dumps({"id": request["id"], "ok": True, "result": {"role": "reader"}}), ""
                    )
                if executable == "InnoCollectorWebServer":
                    self.assertEqual(command[1:], ["--smoke"])
                    return subprocess.CompletedProcess(
                        command, 0, '{"role":"collector-web","protocol":1}\n', ""
                    )
                if command[0] == "strings":
                    return subprocess.CompletedProcess(command, 0, "reader-only", "")
                if "PyInstaller.utils.cliutils.archive_viewer" in command:
                    return subprocess.CompletedProcess(command, 0, WEB_ARCHIVE_CONTENTS, "")
                return subprocess.CompletedProcess(command, 0, "", "")

            build_helpers.build(output=output, moore_source=moore, clean=False, runner=run)

            pyinstaller = [call for call in calls if "PyInstaller" in call]
            self.assertEqual(len(pyinstaller), 4)
            names = {call[call.index("--name") + 1] for call in pyinstaller}
            self.assertEqual(names, set(binaries))
            self.assertTrue(all("--onefile" in call for call in pyinstaller))
            self.assertTrue(all("--collect-all" not in call for call in pyinstaller))
            self.assertEqual(
                len({call[call.index("--distpath") + 1] for call in pyinstaller}), 4
            )
            self.assertEqual(
                len({call[call.index("--workpath") + 1] for call in pyinstaller}), 4
            )
            self.assertEqual(
                len({call[call.index("--specpath") + 1] for call in pyinstaller}), 4
            )
            moore_command = next(call for call in pyinstaller if "MooreExporterHelper" in call)
            self.assertEqual(moore_command[moore_command.index("--paths") + 1], str(moore))
            self.assertTrue(moore_command[-1].endswith("packaging/moore_exporter_entry.py"))
            joined = " ".join(moore_command)
            self.assertNotIn("inspect_context.py", joined)
            self.assertNotIn("wechat_wizard.py", joined)

            source_path = str(ROOT / "src")
            collector_command = next(
                call for call in pyinstaller if "InnoCollectorHelper" in call
            )
            reader_command = next(
                call for call in pyinstaller if "InnoReaderHelper" in call
            )
            self.assertIn(source_path, collector_command)
            self.assertIn(source_path, reader_command)

            web_command = next(
                call for call in pyinstaller if "InnoCollectorWebServer" in call
            )
            self.assertTrue(web_command[-1].endswith("packaging/collector_web_server_entry.py"))
            path_values = [
                web_command[index + 1]
                for index, value in enumerate(web_command)
                if value == "--paths"
            ]
            self.assertEqual(path_values, [source_path, str(moore)])
            hidden_imports = [
                web_command[index + 1]
                for index, value in enumerate(web_command)
                if value == "--hidden-import"
            ]
            self.assertEqual(
                hidden_imports,
                ["wechat_exporter", "wechat_downloader"],
            )
            add_data = [
                web_command[index + 1]
                for index, value in enumerate(web_command)
                if value == "--add-data"
            ]
            self.assertIn(
                f"{ROOT / 'src/inno_collector/web/assets'}:inno_collector/web/assets",
                add_data,
            )
            self.assertIn(
                f"{ROOT / 'src/inno_collector/web/resources'}:inno_collector/web/resources",
                add_data,
            )
            self.assertIn(
                f"{ROOT / 'third_party/licenses'}:ThirdPartyLicenses",
                add_data,
            )
            self.assertTrue(
                all("--add-data" not in call for call in pyinstaller if call is not web_command)
            )

    def test_web_entry_smoke_is_stable_and_does_not_start_the_server(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "packaging/collector_web_server_entry.py"), "--smoke"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"role":"collector-web","protocol":1}\n')
        self.assertEqual(result.stderr, "")

    def test_web_binary_audit_rejects_paths_and_secrets_without_echoing_them(self) -> None:
        local_path = "/Users/private/build/source.py"
        github_token = "ghp_" + "A" * 36

        for leaked_value in (local_path, github_token):
            def run(command, **_kwargs):
                if command[0] == "strings":
                    return subprocess.CompletedProcess(command, 0, leaked_value, "")
                return subprocess.CompletedProcess(command, 0, WEB_ARCHIVE_CONTENTS, "")

            with self.subTest(leaked_value=leaked_value[:4]):
                with self.assertRaises(build_helpers.HelperBuildError) as raised:
                    build_helpers.audit_collector_web_binary(
                        Path("InnoCollectorWebServer"), runner=run
                    )
                self.assertNotIn(leaked_value, str(raised.exception))

    def test_web_binary_audit_requires_assets_projects_and_licenses(self) -> None:
        def run(command, **_kwargs):
            if command[0] == "strings":
                return subprocess.CompletedProcess(command, 0, "safe", "")
            return subprocess.CompletedProcess(
                command,
                0,
                WEB_ARCHIVE_CONTENTS.replace(
                    "inno_collector/web/resources/projects.json", ""
                ),
                "",
            )

        with self.assertRaisesRegex(build_helpers.HelperBuildError, "required resources"):
            build_helpers.audit_collector_web_binary(
                Path("InnoCollectorWebServer"), runner=run
            )

    def test_reader_binary_audit_rejects_collector_only_markers(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["strings"], 0, "safe\nMooreExporterAdapter\n", ""
            )
        )

        with self.assertRaisesRegex(build_helpers.HelperBuildError, "collector-only"):
            build_helpers.audit_reader_binary(Path("ReaderHelper"), runner=runner)

    def test_reader_binary_audit_rejects_web_server_and_project_config(self) -> None:
        for marker in ("InnoCollectorWebServer", "projects.json", "moore_runtime"):
            with self.subTest(marker=marker):
                runner = Mock(
                    return_value=subprocess.CompletedProcess(
                        ["strings"], 0, f"safe\n{marker}\n", ""
                    )
                )
                with self.assertRaisesRegex(
                    build_helpers.HelperBuildError,
                    "collector-only",
                ):
                    build_helpers.audit_reader_binary(
                        Path("ReaderHelper"), runner=runner
                    )

    def test_moore_source_requires_only_the_two_runtime_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "wechat_exporter.py").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(build_helpers.HelperBuildError, "wechat_downloader.py"):
                build_helpers.pyinstaller_commands(Path(temporary) / "out", source)

    def test_developer_id_is_passed_into_every_pyinstaller_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "moore"
            source.mkdir()
            for name in ("wechat_exporter.py", "wechat_downloader.py"):
                (source / name).write_text("", encoding="utf-8")

            commands = build_helpers.pyinstaller_commands(
                root / "out",
                source,
                "Developer ID Application: Example (TEAM123)",
            )

            self.assertTrue(all("--codesign-identity" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
