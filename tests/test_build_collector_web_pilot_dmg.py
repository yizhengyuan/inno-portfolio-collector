from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts import build_collector_web_pilot_dmg


class CollectorWebPilotDmgTests(unittest.TestCase):
    def fake_app(self, root: Path, *, version: str = "0.2.3") -> Path:
        app = root / "InnoCollector.app"
        files = {
            "Contents/MacOS/InnoCollectorApp": b"swift",
            "Contents/PlugIns/InnoCollectorWebServer": b"web",
            "Contents/Resources/config/projects.json": b"{}\n",
            "Contents/Resources/ThirdPartyLicenses/inno-news-suite-LICENSE.txt": b"suite",
            "Contents/Resources/ThirdPartyLicenses/wechat-article-exporter-LICENSE.txt": b"wechat",
            "Contents/Resources/ThirdPartyLicenses/moore-wechat-article-downloader-LICENSE.txt": b"moore",
            "Contents/Resources/ThirdPartyLicenses/THIRD_PARTY_NOTICES.md": b"notices",
        }
        for relative, content in files.items():
            path = app / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (app / "Contents/MacOS/InnoCollectorApp").chmod(0o755)
        (app / "Contents/PlugIns/InnoCollectorWebServer").chmod(0o755)
        info = {
            "CFBundleIdentifier": "com.inno.news.collector",
            "CFBundleExecutable": "InnoCollectorApp",
            "CFBundleShortVersionString": version,
        }
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        return app

    def test_stage_has_exact_top_level_and_complete_chinese_self_use_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.fake_app(root)
            stage = root / "stage"

            build_collector_web_pilot_dmg.stage_pilot_contents(app, stage)

            self.assertEqual(
                {path.name for path in stage.iterdir()},
                {"英诺资讯采集.app", "Applications", "安装说明.txt"},
            )
            applications = stage / "Applications"
            self.assertTrue(applications.is_symlink())
            self.assertEqual(applications.readlink().as_posix(), "/Applications")
            notice = (stage / "安装说明.txt").read_text(encoding="utf-8")
            for required in (
                "英诺资讯采集.app",
                "本地网页",
                "不是云服务",
                "关闭浏览器",
                "退出 App",
                "本机",
                "ad-hoc",
                "未经 Apple 公证",
                "仅供本人使用",
                "不得转发",
                "旧 r3",
            ):
                self.assertIn(required, notice)
            self.assertEqual(
                {
                    path.name
                    for path in (
                        stage / "英诺资讯采集.app/Contents/PlugIns"
                    ).iterdir()
                },
                {"InnoCollectorWebServer"},
            )

    def test_build_uses_app_version_and_date_for_one_dmg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.fake_app(root, version="0.2.7")
            output = root / "dist"
            commands: list[list[str]] = []
            staged_names: set[str] = set()

            def run(command, **_kwargs):
                command = [str(value) for value in command]
                commands.append(command)
                if command[0] == "hdiutil":
                    source = Path(command[command.index("-srcfolder") + 1])
                    staged_names.update(path.name for path in source.iterdir())
                    Path(command[-1]).write_bytes(b"dmg")
                return subprocess.CompletedProcess(command, 0, "", "")

            dmg = build_collector_web_pilot_dmg.build_pilot_dmg(
                app=app,
                output=output,
                build_date=date(2026, 7, 13),
                runner=run,
            )

            self.assertEqual(
                dmg.name,
                "InnoCollector-Web-0.2.7-pilot-20260713.dmg",
            )
            self.assertEqual(sorted(output.iterdir()), [dmg])
            self.assertEqual(
                staged_names,
                {"英诺资讯采集.app", "Applications", "安装说明.txt"},
            )
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][0:3], ["codesign", "--verify", "--deep"])
            self.assertEqual(commands[1][0:2], ["hdiutil", "create"])
            self.assertIn("UDZO", commands[1])

    def test_rejects_unsafe_version_and_legacy_collector_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe_version = self.fake_app(root / "unsafe", version="../../private")
            with self.assertRaisesRegex(
                build_collector_web_pilot_dmg.PilotDmgError,
                "version",
            ):
                build_collector_web_pilot_dmg.pilot_dmg_name(
                    unsafe_version,
                    date(2026, 7, 13),
                )

            legacy_app = self.fake_app(root / "legacy")
            legacy = legacy_app / "Contents/PlugIns/InnoCollectorHelper"
            legacy.write_bytes(b"legacy")
            with self.assertRaisesRegex(
                build_collector_web_pilot_dmg.PilotDmgError,
                "layout",
            ):
                build_collector_web_pilot_dmg.stage_pilot_contents(
                    legacy_app,
                    root / "stage",
                )

            non_executable = self.fake_app(root / "non-executable")
            (non_executable / "Contents/MacOS/InnoCollectorApp").chmod(0o644)
            with self.assertRaisesRegex(
                build_collector_web_pilot_dmg.PilotDmgError,
                "executable",
            ):
                build_collector_web_pilot_dmg.stage_pilot_contents(
                    non_executable,
                    root / "non-executable-stage",
                )


if __name__ == "__main__":
    unittest.main()
