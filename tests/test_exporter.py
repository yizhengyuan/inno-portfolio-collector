from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from inno_collector.exporter import (
    ExporterCommandError,
    MooreExporterAdapter,
    _default_runner,
    _sanitize,
)
from inno_collector.models import ProjectAccount


class FakeRunner:
    def __init__(self, *responses: tuple[int, str, str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.calls.append(command.copy())
        return self.responses.pop(0)


class MooreExporterAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = Path("/opt/moore-exporter/exporter.py")
        self.runtime_dir = Path("/tmp/moore runtime")

    def adapter(self, runner: FakeRunner) -> MooreExporterAdapter:
        return MooreExporterAdapter(self.script, self.runtime_dir, runner=runner)

    def test_auth_check_builds_command_and_parses_json(self) -> None:
        payload = {"ok": True, "authenticated": True}
        runner = FakeRunner((0, json.dumps(payload), ""))

        result = self.adapter(runner).auth_check()

        self.assertEqual(result, payload)
        self.assertEqual(
            runner.calls,
            [
                [
                    sys.executable,
                    str(self.script),
                    "--runtime-dir",
                    str(self.runtime_dir),
                    "exporter-auth-check",
                ]
            ],
        )

    def test_nonzero_exit_raises_a_sanitized_error(self) -> None:
        runner = FakeRunner(
            (2, json.dumps({"ok": True}), "login failed pass_ticket=secret")
        )

        with self.assertRaises(ExporterCommandError) as raised:
            self.adapter(runner).auth_check()

        self.assertNotIn("secret", str(raised.exception))
        self.assertIn("pass_ticket=[REDACTED]", str(raised.exception))

    def test_unsuccessful_payload_raises_a_sanitized_error(self) -> None:
        runner = FakeRunner(
            (
                0,
                json.dumps({"ok": False, "error": "bad auth-key=topsecret"}),
                "",
            )
        )

        with self.assertRaises(ExporterCommandError) as raised:
            self.adapter(runner).auth_check()

        self.assertNotIn("topsecret", str(raised.exception))
        self.assertIn("auth-key=[REDACTED]", str(raised.exception))

    def test_non_json_stdout_raises_a_sanitized_error(self) -> None:
        runner = FakeRunner((0, "not json token=invalid-json-secret", ""))

        with self.assertRaises(ExporterCommandError) as raised:
            self.adapter(runner).auth_check()

        self.assertNotIn("invalid-json-secret", str(raised.exception))
        self.assertIn("token=[REDACTED]", str(raised.exception))

    def test_collection_commands_use_default_limits_and_parse_returns(self) -> None:
        accounts = [{"id": 11, "nickname": "Alpha"}]
        sync_payload = {"ok": True, "synced": 4}
        articles = [{"id": 21}, {"id": 22}]
        download_payload = {"ok": True, "downloaded": 2}
        runner = FakeRunner(
            (0, json.dumps({"ok": True, "accounts": accounts}), ""),
            (0, json.dumps(sync_payload), ""),
            (0, json.dumps({"ok": True, "articles": articles}), ""),
            (0, json.dumps(download_payload), ""),
        )
        adapter = self.adapter(runner)
        output_root = Path("/tmp/article output")

        self.assertEqual(adapter.accounts(), accounts)
        self.assertEqual(adapter.sync(11), sync_payload)
        self.assertEqual(adapter.articles(11), articles)
        self.assertEqual(adapter.download([21, 22], output_root), download_payload)
        prefix = [
            sys.executable,
            str(self.script),
            "--runtime-dir",
            str(self.runtime_dir),
        ]
        self.assertEqual(
            runner.calls,
            [
                [*prefix, "exporter-accounts"],
                [
                    *prefix,
                    "exporter-sync",
                    "--account-id",
                    "11",
                    "--limit",
                    "1000",
                ],
                [
                    *prefix,
                    "exporter-articles",
                    "--account-id",
                    "11",
                    "--limit",
                    "5000",
                ],
                [
                    *prefix,
                    "exporter-download",
                    "--article-ids",
                    "21,22",
                    "--output-dir",
                    str(output_root),
                ],
            ],
        )

    def test_collection_commands_accept_explicit_limits(self) -> None:
        runner = FakeRunner(
            (0, json.dumps({"ok": True}), ""),
            (0, json.dumps({"ok": True}), ""),
        )
        adapter = self.adapter(runner)

        adapter.sync(7, limit=12)
        adapter.articles(7, limit=34)

        self.assertEqual(runner.calls[0][-4:], ["--account-id", "7", "--limit", "12"])
        self.assertEqual(runner.calls[1][-4:], ["--account-id", "7", "--limit", "34"])

    def test_resolve_exact_matches_each_configured_identifier(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Official Name",
            wechat_id="wx_alpha",
            aliases=("Alpha Labs",),
        )
        cases = (
            ({"id": 1, "nickname": "  OFFICIAL NAME ", "alias": "other"}, 1),
            ({"id": 2, "nickname": "other", "alias": " WX_ALPHA "}, 2),
            ({"id": 3, "nickname": " alpha LABS ", "alias": "other"}, 3),
        )
        adapter = self.adapter(FakeRunner())

        for row, expected_id in cases:
            with self.subTest(expected_id=expected_id):
                self.assertEqual(adapter.resolve_exact(project, [row])["id"], expected_id)

    def test_resolve_exact_rejects_zero_matches_without_fuzzy_matching(self) -> None:
        project = ProjectAccount(project="Project A", account="Alpha")
        rows = [{"id": 1, "nickname": "Alpha Plus", "alias": "alpha_official"}]

        with self.assertRaisesRegex(
            ExporterCommandError,
            "^expected one exact account match for Project A, got 0$",
        ):
            self.adapter(FakeRunner()).resolve_exact(project, rows)

    def test_resolve_exact_rejects_multiple_matching_rows(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Alpha",
            wechat_id="alpha_id",
        )
        rows = [
            {"id": 1, "nickname": "alpha", "alias": "first"},
            {"id": 2, "nickname": "second", "alias": "ALPHA_ID"},
        ]

        with self.assertRaisesRegex(
            ExporterCommandError,
            "^expected one exact account match for Project A, got 2$",
        ):
            self.adapter(FakeRunner()).resolve_exact(project, rows)

    def test_sanitize_redacts_all_supported_key_value_secrets(self) -> None:
        secrets = (
            "secret-auth",
            "secret-pass",
            "secret-appmsg",
            "secret-token",
            "secret-ticket",
            "secret-uin",
        )
        message = (
            "auth-key=secret-auth PASS_TICKET=secret-pass "
            "appmsg_token=secret-appmsg ToKeN=secret-token "
            "ticket=secret-ticket UIN=secret-uin safe=value"
        )

        sanitized = _sanitize(message)

        for secret in secrets:
            self.assertNotIn(secret, sanitized)
        self.assertEqual(sanitized.count("[REDACTED]"), 6)
        self.assertIn("safe=value", sanitized)

    def test_default_runner_captures_stdout_stderr_and_exit_code(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ]

        result = _default_runner(command)

        self.assertEqual(result, (3, "out\n", "err\n"))


if __name__ == "__main__":
    unittest.main()
