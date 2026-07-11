from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import build_helpers


class BuildHelperTests(unittest.TestCase):
    def test_three_independent_pyinstaller_commands_and_smokes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "helpers"
            moore = Path(temporary) / "moore"
            moore.mkdir()
            for name in ("wechat_exporter.py", "wechat_downloader.py"):
                (moore / name).write_text("", encoding="utf-8")
            binaries = {
                "InnoCollectorHelper": output / "collector/InnoCollectorHelper",
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
                if command[0] == "strings":
                    return subprocess.CompletedProcess(command, 0, "reader-only", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            build_helpers.build(output=output, moore_source=moore, clean=False, runner=run)

            pyinstaller = [call for call in calls if "PyInstaller" in call]
            self.assertEqual(len(pyinstaller), 3)
            names = {call[call.index("--name") + 1] for call in pyinstaller}
            self.assertEqual(names, set(binaries))
            self.assertTrue(all("--onefile" in call for call in pyinstaller))
            self.assertTrue(all("--collect-all" not in call for call in pyinstaller))
            self.assertEqual(
                len({call[call.index("--distpath") + 1] for call in pyinstaller}), 3
            )
            self.assertEqual(
                len({call[call.index("--workpath") + 1] for call in pyinstaller}), 3
            )
            self.assertEqual(
                len({call[call.index("--specpath") + 1] for call in pyinstaller}), 3
            )
            moore_command = next(call for call in pyinstaller if "MooreExporterHelper" in call)
            self.assertEqual(moore_command[moore_command.index("--paths") + 1], str(moore))
            self.assertTrue(moore_command[-1].endswith("packaging/moore_exporter_entry.py"))
            joined = " ".join(moore_command)
            self.assertNotIn("inspect_context.py", joined)
            self.assertNotIn("wechat_wizard.py", joined)

    def test_reader_binary_audit_rejects_collector_only_markers(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["strings"], 0, "safe\nMooreExporterAdapter\n", ""
            )
        )

        with self.assertRaisesRegex(build_helpers.HelperBuildError, "collector-only"):
            build_helpers.audit_reader_binary(Path("ReaderHelper"), runner=runner)

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
