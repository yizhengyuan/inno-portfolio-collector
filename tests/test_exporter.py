from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

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

    def test_unsuccessful_payload_without_details_uses_stable_error(self) -> None:
        runner = FakeRunner((0, json.dumps({"ok": False}), ""))

        with self.assertRaises(ExporterCommandError) as raised:
            self.adapter(runner).auth_check()

        self.assertEqual(str(raised.exception), "exporter command failed")

    def test_non_object_json_payload_raises_stable_protocol_error(self) -> None:
        for payload in ([], None):
            with self.subTest(payload=payload):
                runner = FakeRunner((0, json.dumps(payload), ""))

                with self.assertRaises(ExporterCommandError) as raised:
                    self.adapter(runner).auth_check()

                self.assertEqual(
                    str(raised.exception), "exporter returned invalid JSON object"
                )

    def test_string_ok_values_never_succeed(self) -> None:
        for ok_value in ("false", "true"):
            with self.subTest(ok=ok_value):
                runner = FakeRunner((0, json.dumps({"ok": ok_value}), ""))

                with self.assertRaises(ExporterCommandError) as raised:
                    self.adapter(runner).auth_check()

                self.assertEqual(str(raised.exception), "exporter command failed")

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
            (0, json.dumps({"ok": True, "articles": []}), ""),
        )
        adapter = self.adapter(runner)

        adapter.sync(7, limit=12)
        adapter.articles(7, limit=34)

        self.assertEqual(runner.calls[0][-4:], ["--account-id", "7", "--limit", "12"])
        self.assertEqual(runner.calls[1][-4:], ["--account-id", "7", "--limit", "34"])

    def test_accounts_rejects_invalid_collections(self) -> None:
        for accounts in ("not-a-list", [{"id": 1}, "not-an-object"]):
            with self.subTest(accounts=accounts):
                runner = FakeRunner(
                    (0, json.dumps({"ok": True, "accounts": accounts}), "")
                )

                with self.assertRaises(ExporterCommandError) as raised:
                    self.adapter(runner).accounts()

                self.assertEqual(
                    str(raised.exception), "exporter returned invalid accounts"
                )

    def test_articles_rejects_invalid_collections(self) -> None:
        for articles in ({"id": 1}, [{"id": 1}, 2]):
            with self.subTest(articles=articles):
                runner = FakeRunner(
                    (0, json.dumps({"ok": True, "articles": articles}), "")
                )

                with self.assertRaises(ExporterCommandError) as raised:
                    self.adapter(runner).articles(7)

                self.assertEqual(
                    str(raised.exception), "exporter returned invalid articles"
                )

    def test_empty_account_and_article_lists_are_allowed(self) -> None:
        runner = FakeRunner(
            (0, json.dumps({"ok": True, "accounts": []}), ""),
            (0, json.dumps({"ok": True, "articles": []}), ""),
        )
        adapter = self.adapter(runner)

        self.assertEqual(adapter.accounts(), [])
        self.assertEqual(adapter.articles(7), [])

    def test_collection_fields_must_be_present(self) -> None:
        for field in ("accounts", "articles"):
            with self.subTest(field=field):
                runner = FakeRunner((0, json.dumps({"ok": True}), ""))
                adapter = self.adapter(runner)

                with self.assertRaises(ExporterCommandError) as raised:
                    if field == "accounts":
                        adapter.accounts()
                    else:
                        adapter.articles(7)

                self.assertEqual(
                    str(raised.exception), f"exporter returned invalid {field}"
                )

    def test_large_successful_articles_payload_is_not_truncated(self) -> None:
        articles = [{"id": 1, "body": "x" * 10000}]
        runner = FakeRunner(
            (0, json.dumps({"ok": True, "articles": articles}), "")
        )

        self.assertEqual(self.adapter(runner).articles(7), articles)

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

    def test_resolve_exact_rejects_identifiers_in_crossed_fields(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Official Name",
            wechat_id="wx_alpha",
            aliases=("Alpha Labs",),
        )
        rows = (
            {"id": 1, "nickname": "WX_ALPHA", "alias": "other"},
            {"id": 2, "nickname": "other", "alias": "official name"},
            {"id": 3, "nickname": "other", "alias": "alpha labs"},
        )
        adapter = self.adapter(FakeRunner())

        for row in rows:
            with self.subTest(row=row):
                with self.assertRaisesRegex(
                    ExporterCommandError,
                    "^expected one exact account match for Project A, got 0$",
                ):
                    adapter.resolve_exact(project, [row])

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

    def test_sanitize_redacts_common_secret_formats(self) -> None:
        cases = (
            "token: colon-secret",
            '"token": "json-secret"',
            'token="quoted-secret"',
            "Authorization: Bearer bearer-secret",
            "--auth-key cli-secret",
        )

        for message in cases:
            with self.subTest(message=message):
                sanitized = _sanitize(message)
                self.assertNotIn("secret", sanitized)
                self.assertIn("[REDACTED]", sanitized)

                runner = FakeRunner(
                    (0, json.dumps({"ok": False, "error": message}), "")
                )
                with self.assertRaises(ExporterCommandError) as raised:
                    self.adapter(runner).auth_check()
                self.assertNotIn("secret", str(raised.exception))

    def test_sanitize_redacts_quoted_authorization_formats(self) -> None:
        cases = (
            ('"Authorization": "Bearer json-secret"', "json-secret"),
            ("{'Authorization': 'Bearer dict-secret'}", "dict-secret"),
        )

        for message, secret in cases:
            with self.subTest(message=message):
                sanitized = _sanitize(message)
                self.assertNotIn(secret, sanitized)
                self.assertIn("[REDACTED]", sanitized)

                runner = FakeRunner(
                    (0, json.dumps({"ok": False, "error": message}), "")
                )
                with self.assertRaises(ExporterCommandError) as raised:
                    self.adapter(runner).auth_check()
                self.assertNotIn(secret, str(raised.exception))
                self.assertIn("[REDACTED]", str(raised.exception))

    def test_error_diagnostic_length_is_bounded(self) -> None:
        runner = FakeRunner((2, json.dumps({"ok": True}), "failure: " + "x" * 10000))

        with self.assertRaises(ExporterCommandError) as raised:
            self.adapter(runner).auth_check()

        message = str(raised.exception)
        self.assertLessEqual(len(message), 4096)
        self.assertTrue(message.startswith("failure: "))

    def test_timeout_is_converted_without_command_details(self) -> None:
        def timeout_runner(command: list[str]) -> tuple[int, str, str]:
            raise subprocess.TimeoutExpired(
                [*command, "--auth-key", "timeout-secret"],
                timeout=300,
            )

        adapter = MooreExporterAdapter(
            self.script,
            self.runtime_dir,
            runner=timeout_runner,
        )

        with self.assertRaises(ExporterCommandError) as raised:
            adapter.auth_check()

        self.assertEqual(str(raised.exception), "exporter command timed out")

    def test_default_runner_captures_stdout_stderr_and_exit_code(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ]

        result = _default_runner(command)

        self.assertEqual(result, (3, "out\n", "err\n"))

    @patch("inno_collector.exporter.subprocess.run")
    def test_default_runner_sets_subprocess_timeout(self, run: Mock) -> None:
        def write_output(command: list[str], **kwargs: Any) -> object:
            kwargs["stdout"].write("out")
            kwargs["stderr"].write("err")
            return subprocess.CompletedProcess(args=command, returncode=0)

        run.side_effect = write_output
        command = ["exporter", "auth"]

        result = _default_runner(command)

        self.assertEqual(result, (0, "out", "err"))
        args, kwargs = run.call_args
        self.assertEqual(args, (command,))
        self.assertIs(kwargs["text"], True)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["timeout"], 300)
        self.assertIn("stdout", kwargs)
        self.assertIn("stderr", kwargs)
        self.assertTrue(hasattr(kwargs["stdout"], "write"))
        self.assertTrue(hasattr(kwargs["stderr"], "write"))
        self.assertNotIn("capture_output", kwargs)

    def test_output_over_limit_becomes_stable_error(self) -> None:
        cases = (
            ("stdout", "MAX_STDOUT_BYTES"),
            ("stderr", "MAX_STDERR_BYTES"),
        )
        for stream_name, limit_name in cases:
            with self.subTest(stream=stream_name):
                def write_oversized_output(
                    command: list[str], **kwargs: Any
                ) -> object:
                    kwargs[stream_name].write("oversized-secret-output")
                    return subprocess.CompletedProcess(args=command, returncode=0)

                with patch(f"inno_collector.exporter.{limit_name}", 8), patch(
                    "inno_collector.exporter.subprocess.run"
                ) as run:
                    run.side_effect = write_oversized_output
                    adapter = MooreExporterAdapter(self.script, self.runtime_dir)

                    with self.assertRaises(ExporterCommandError) as raised:
                        adapter.auth_check()

                self.assertEqual(
                    str(raised.exception), "exporter output exceeded safe limit"
                )
                self.assertNotIn("oversized-secret-output", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
