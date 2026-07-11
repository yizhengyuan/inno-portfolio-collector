from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_swift.sh"


class SwiftTestScriptTests(unittest.TestCase):
    def run_script(self, developer_root: Path) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bin_directory = Path(temporary_directory) / "bin"
            bin_directory.mkdir()
            swift = bin_directory / "swift"
            swift.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
                encoding="utf-8",
            )
            swift.chmod(swift.stat().st_mode | stat.S_IXUSR)

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_directory}{os.pathsep}{environment['PATH']}"
            environment["DEVELOPER_DIR"] = str(developer_root)
            result = subprocess.run(
                [SCRIPT],
                check=True,
                capture_output=True,
                env=environment,
                text=True,
            )

        return result.stdout.splitlines()

    def test_command_line_tools_adds_testing_framework_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            developer_root = Path(temporary_directory) / "CommandLineTools"
            frameworks = developer_root / "Library" / "Developer" / "Frameworks"
            (frameworks / "Testing.framework").mkdir(parents=True)
            (developer_root / "Library" / "Developer" / "usr" / "lib").mkdir(
                parents=True
            )

            arguments = self.run_script(developer_root)

        self.assertIn("-Xswiftc", arguments)
        self.assertIn(str(frameworks), arguments)

    def test_full_xcode_uses_plain_swift_package_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            developer_root = (
                Path(temporary_directory) / "Xcode.app" / "Contents" / "Developer"
            )
            developer_root.mkdir(parents=True)

            arguments = self.run_script(developer_root)

        self.assertNotIn("-Xswiftc", arguments)
        self.assertEqual(
            arguments[:3],
            ["test", "--enable-swift-testing", "--disable-xctest"],
        )


if __name__ == "__main__":
    unittest.main()
