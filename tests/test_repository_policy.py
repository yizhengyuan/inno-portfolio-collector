from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import check_repository_policy
from scripts.check_repository_policy import PolicyViolation, audit_repository


REQUIRED = {
    "LICENSE": b"MIT License\nPermission is hereby granted\n",
    "SECURITY.md": "私密漏洞报告".encode("utf-8"),
    "THIRD_PARTY_NOTICES.md": (
        b"wechat-article-exporter\nmoore-wechat-article-downloader\n"
    ),
    "third_party/licenses/moore-wechat-article-downloader-LICENSE.txt": (
        b"MIT License\n"
    ),
    "third_party/licenses/wechat-article-exporter-LICENSE.txt": b"MIT License\n",
    "packaging/collector_web_server_entry.py": (
        b"from inno_collector.web.server import main\n"
    ),
}


def audit(files: dict[str, bytes], paths: list[str] | None = None) -> list[PolicyViolation]:
    tracked = list(files) if paths is None else paths
    return audit_repository(tracked, files.__getitem__)


class RepositoryPolicyTests(unittest.TestCase):
    def test_clean_repository_allows_ordinary_test_fixture_secret(self) -> None:
        files = {**REQUIRED, "tests/fixtures/example.txt": b"token=fixture-secret"}

        self.assertEqual(audit(files), [])

    def test_missing_license_is_reported(self) -> None:
        files = {path: content for path, content in REQUIRED.items() if path != "LICENSE"}

        self.assertEqual(
            audit(files),
            [PolicyViolation("LICENSE", "required-file-missing")],
        )

    def test_required_file_with_missing_marker_is_reported(self) -> None:
        files = {**REQUIRED, "LICENSE": b"MIT License\n"}

        self.assertEqual(
            audit(files),
            [PolicyViolation("LICENSE", "required-marker-missing")],
        )

    def test_unified_web_server_entry_is_required(self) -> None:
        files = {
            path: content
            for path, content in REQUIRED.items()
            if path != "packaging/collector_web_server_entry.py"
        }

        self.assertEqual(
            audit(files),
            [
                PolicyViolation(
                    "packaging/collector_web_server_entry.py",
                    "required-file-missing",
                )
            ],
        )

    def test_unified_web_server_entry_must_target_the_local_server(self) -> None:
        files = {
            **REQUIRED,
            "packaging/collector_web_server_entry.py": b"def main(): return 0\n",
        }

        self.assertEqual(
            audit(files),
            [
                PolicyViolation(
                    "packaging/collector_web_server_entry.py",
                    "required-marker-missing",
                )
            ],
        )

    def test_unified_web_server_entry_must_not_launch_a_browser(self) -> None:
        files = {
            **REQUIRED,
            "packaging/collector_web_server_entry.py": (
                b"from inno_collector.web.server import main\n"
                b"import webbrowser\n"
            ),
        }

        self.assertEqual(
            audit(files),
            [
                PolicyViolation(
                    "packaging/collector_web_server_entry.py",
                    "web-server-entry-launches-browser",
                )
            ],
        )

    def test_user_material_and_credential_paths_are_stably_sorted_and_unique(self) -> None:
        files = {
            **REQUIRED,
            ".superpowers/session.json": b"{}",
            "英诺项目清单-2026/source.xlsx": b"spreadsheet",
            ".env.production": b"DEBUG=false",
            "certificates/distribution.p12": b"certificate",
        }
        paths = [
            "英诺项目清单-2026/source.xlsx",
            ".superpowers/session.json",
            *REQUIRED,
            "certificates/distribution.p12",
            ".env.production",
            ".env.production",
        ]
        expected = sorted(
            {
                PolicyViolation(".superpowers/session.json", "user-material"),
                PolicyViolation("英诺项目清单-2026/source.xlsx", "user-material"),
                PolicyViolation(".env.production", "credential-file"),
                PolicyViolation("certificates/distribution.p12", "credential-file"),
            }
        )

        self.assertEqual(audit(files, paths), expected)

    def test_all_user_material_prefixes_are_rejected(self) -> None:
        paths = [
            ".superpowers/notes.txt",
            "英诺项目清单-2026/source.xlsx",
            "runtime/cache.json",
            ".moore/account.json",
            "ExporterRuntime/login/session.json",
            "DraftInbox/draft.zip",
            "DeliveryTemp/baseline.inno-update",
            "UploadTemp/upload.bin",
        ]
        files = {**REQUIRED, **dict.fromkeys(paths, b"fixture")}

        self.assertEqual(
            audit(files),
            sorted(PolicyViolation(path, "user-material") for path in paths),
        )

    def test_all_credential_filename_forms_are_rejected(self) -> None:
        paths = [
            ".env",
            "config/.env.local",
            "keys/id_rsa",
            "keys/id_ed25519",
            "private/credential",
            "private/credentials",
            "private/secret",
            "private/secrets",
            "keys/client.pem",
            "keys/client.key",
            "keys/client.p12",
            "keys/client.pfx",
            "state/cookies.sqlite",
            "state/auth-key",
            "state/auth_key",
        ]
        files = {**REQUIRED, **dict.fromkeys(paths, b"fixture")}

        self.assertEqual(
            audit(files),
            sorted(PolicyViolation(path, "credential-file") for path in paths),
        )

    def test_frozen_build_outputs_are_never_tracked(self) -> None:
        paths = [
            "build/collector-web/InnoCollectorWebServer",
            "dist/InnoCollector-Web-pilot.dmg",
            "macos/.build/release/InnoCollectorApp",
            ".build-macos/InnoCollector.app/Contents/MacOS/InnoCollectorApp",
            "packaging/InnoCollectorWebServer.spec",
        ]
        files = {**REQUIRED, **dict.fromkeys(paths, b"binary fixture")}

        self.assertEqual(
            audit(files),
            sorted(PolicyViolation(path, "build-artifact") for path in paths),
        )

    def test_release_inputs_reject_build_machine_absolute_paths(self) -> None:
        user_path = b"/Users/build-user/work/inno/config.json"
        volume_path = b"/Volumes/Build Disk/output/InnoCollectorWebServer"
        files = {
            **REQUIRED,
            "packaging/frozen-config.txt": user_path,
            "src/inno_collector/web/resources/build-origin.txt": volume_path,
        }

        violations = audit(files)

        self.assertEqual(
            violations,
            sorted(
                [
                    PolicyViolation(
                        "packaging/frozen-config.txt",
                        "build-machine-path",
                    ),
                    PolicyViolation(
                        "src/inno_collector/web/resources/build-origin.txt",
                        "build-machine-path",
                    ),
                ]
            ),
        )
        rendered = repr(violations)
        self.assertNotIn(user_path.decode("ascii"), rendered)
        self.assertNotIn(volume_path.decode("ascii"), rendered)

    def test_docs_and_tests_may_use_obviously_synthetic_absolute_paths(self) -> None:
        files = {
            **REQUIRED,
            "docs/example.md": b"/Users/example/project/fixture",
            "tests/test_path_fixture.py": b"ROOT = '/Volumes/Test/fixture'",
        }

        self.assertEqual(audit(files), [])

    def test_release_inputs_reject_embedded_credential_values(self) -> None:
        auth_key = b"A" * 32
        cookie = b"B" * 32
        files = {
            **REQUIRED,
            "packaging/frozen-settings.py": b'AUTH_KEY = "' + auth_key + b'"',
            "src/inno_collector/web/resources/session.json": (
                b'{"cookie":"' + cookie + b'"}'
            ),
        }

        violations = audit(files)

        self.assertEqual(
            violations,
            sorted(
                [
                    PolicyViolation(
                        "packaging/frozen-settings.py",
                        "embedded-credential",
                    ),
                    PolicyViolation(
                        "src/inno_collector/web/resources/session.json",
                        "embedded-credential",
                    ),
                ]
            ),
        )
        rendered = repr(violations)
        self.assertNotIn(auth_key.decode("ascii"), rendered)
        self.assertNotIn(cookie.decode("ascii"), rendered)

    def test_release_source_allows_credential_field_names_without_values(self) -> None:
        files = {
            **REQUIRED,
            "src/inno_collector/web/security.py": (
                b"# redact auth-key, cookie, and token fields before returning\n"
            ),
        }

        self.assertEqual(audit(files), [])

    def test_reader_components_reject_collector_only_capabilities(self) -> None:
        files = {
            **REQUIRED,
            "macos/Sources/InnoReaderFeature/WebLeak.swift": (
                b"let helper = \"InnoCollectorWebServer\""
            ),
            "src/inno_collector/reader_helper.py": (
                b"from moore_runtime import MooreRuntime"
            ),
            "packaging/reader_helper_entry.py": b"HEADER = 'auth-key'",
            "macos/Sources/InnoReaderApp/ProjectLeak.swift": (
                b"let config = \"config/projects.json\""
            ),
        }

        self.assertEqual(
            audit(files),
            sorted(
                [
                    PolicyViolation(
                        "macos/Sources/InnoReaderFeature/WebLeak.swift",
                        "reader-web-server",
                    ),
                    PolicyViolation(
                        "src/inno_collector/reader_helper.py",
                        "reader-moore-runtime",
                    ),
                    PolicyViolation(
                        "packaging/reader_helper_entry.py",
                        "reader-auth-key",
                    ),
                    PolicyViolation(
                        "macos/Sources/InnoReaderApp/ProjectLeak.swift",
                        "reader-project-config",
                    ),
                ]
            ),
        )

    def test_cutover_rejects_legacy_collector_entries(self) -> None:
        legacy = {
            "macos/Sources/InnoCollectorFeature/CollectorContentView.swift": b"legacy",
            "macos/Sources/InnoCollectorFeature/CollectorViewModel.swift": b"legacy",
            "macos/Sources/InnoCollectorFeature/MooreLocalLoginServer.swift": b"legacy",
            "packaging/collector_helper_entry.py": b"legacy",
            "packaging/moore_exporter_entry.py": b"legacy",
            "src/inno_collector/collector_helper.py": b"legacy",
        }

        self.assertEqual(
            audit({**REQUIRED, **legacy}),
            sorted(
                PolicyViolation(path, "legacy-collector-entry")
                for path in legacy
            ),
        )

    def test_credential_words_inside_ordinary_filenames_are_allowed(self) -> None:
        files = {
            **REQUIRED,
            "docs/secret-management.md": b"fixture",
            "tests/credentials-template.json": b"{}",
        }

        self.assertEqual(audit(files), [])

    def test_high_confidence_secret_patterns_are_reported_without_secret_values(self) -> None:
        github_token = "ghp_" + "A" * 36
        aws_key = "AKIA" + "B" * 16
        private_key_header = b"-----BEGIN " + b"PRIVATE KEY-----"
        files = {
            **REQUIRED,
            "fixtures/github.txt": github_token.encode("ascii"),
            "fixtures/aws.txt": aws_key.encode("ascii"),
            "fixtures/private.txt": private_key_header,
        }
        expected = sorted(
            [
                PolicyViolation("fixtures/github.txt", "github-token"),
                PolicyViolation("fixtures/aws.txt", "aws-access-key"),
                PolicyViolation("fixtures/private.txt", "private-key"),
            ]
        )

        violations = audit(files)

        self.assertEqual(violations, expected)
        rendered = repr(violations)
        self.assertNotIn(github_token, rendered)
        self.assertNotIn(aws_key, rendered)
        self.assertNotIn(private_key_header.decode("ascii"), rendered)

    def test_github_token_allows_underscore_in_twenty_character_payload(self) -> None:
        github_token = b"ghp_" + b"A" * 9 + b"_" + b"B" * 10
        files = {**REQUIRED, "fixtures/github.txt": github_token}

        self.assertEqual(
            audit(files),
            [PolicyViolation("fixtures/github.txt", "github-token")],
        )

    def test_github_token_rejects_nineteen_character_payload(self) -> None:
        github_token = b"ghp_" + b"A" * 9 + b"_" + b"B" * 9
        files = {**REQUIRED, "fixtures/github.txt": github_token}

        self.assertEqual(audit(files), [])

    def test_binary_and_oversized_files_skip_content_scanning(self) -> None:
        github_token = ("ghp_" + "C" * 36).encode("ascii")
        aws_key = ("AKIA" + "D" * 16).encode("ascii")
        files = {
            **REQUIRED,
            "fixtures/binary.dat": b"\x00" + github_token,
            "fixtures/large.txt": b"x" * 2_000_001 + aws_key,
        }

        self.assertEqual(audit(files), [])

    def test_unreadable_tracked_file_is_reported(self) -> None:
        files = {**REQUIRED, "broken.txt": b"ignored"}

        def read_bytes(path: str) -> bytes:
            if path == "broken.txt":
                raise OSError("fixture read failure")
            return files[path]

        self.assertEqual(
            audit_repository(files, read_bytes),
            [PolicyViolation("broken.txt", "tracked-file-unreadable")],
        )

    def test_main_scans_a_tracked_symlink_without_following_its_target(self) -> None:
        github_token = ("ghp_" + "E" * 36).encode("ascii")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            for path, content in REQUIRED.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            outside = Path(temporary) / "outside.txt"
            outside.write_bytes(github_token)
            (root / "tracked-link").symlink_to(outside)
            paths = [*REQUIRED, "tracked-link"]
            output = io.StringIO()

            with (
                patch.object(check_repository_policy, "ROOT", root),
                patch.object(check_repository_policy, "tracked_paths", return_value=paths),
                redirect_stdout(output),
            ):
                result = check_repository_policy.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue(),
            f"repository policy passed: {len(paths)} tracked files\n",
        )
        self.assertNotIn(github_token.decode("ascii"), output.getvalue())

    def test_main_failure_output_never_includes_the_matched_value(self) -> None:
        github_token = ("ghp_" + "F" * 36).encode("ascii")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {**REQUIRED, "fixtures/github.txt": github_token}
            for path, content in files.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            output = io.StringIO()

            with (
                patch.object(check_repository_policy, "ROOT", root),
                patch.object(
                    check_repository_policy,
                    "tracked_paths",
                    return_value=list(files),
                ),
                redirect_stdout(output),
            ):
                result = check_repository_policy.main()

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "fixtures/github.txt: github-token\n")
        self.assertNotIn(github_token.decode("ascii"), output.getvalue())

    def test_tracked_paths_decodes_nul_delimited_utf8(self) -> None:
        root = Path("/fixture/repository")
        output = b"README.md\x00" + "文档.md".encode("utf-8") + b"\x00"
        completed = subprocess.CompletedProcess(
            ["git", "ls-files", "-z"],
            0,
            stdout=output,
        )

        with patch.object(
            check_repository_policy.subprocess,
            "run",
            return_value=completed,
        ) as run:
            paths = check_repository_policy.tracked_paths(root)

        self.assertEqual(paths, ["README.md", "文档.md"])
        run.assert_called_once_with(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
