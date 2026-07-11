from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from inno_collector.collector_helper import HANDLERS as COLLECTOR_HANDLERS
from inno_collector.helper_protocol import run_helper
from inno_collector.reader_helper import HANDLERS as READER_HANDLERS


class HelperProtocolTests(unittest.TestCase):
    def call(self, handlers, request: object) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        code = run_helper(handlers, io.StringIO(json.dumps(request)), output)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0]), output.getvalue()

    def test_success_returns_matching_id_and_result(self) -> None:
        code, response, _raw = self.call(
            {"echo": lambda arguments: {"value": arguments["value"]}},
            {"id": "request-1", "command": "echo", "arguments": {"value": "好"}},
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            response,
            {"id": "request-1", "ok": True, "result": {"value": "好"}},
        )

    def test_invalid_and_unsupported_requests_return_one_stable_error(self) -> None:
        for request in (
            [],
            {"id": "r", "command": "missing", "arguments": {}},
            {"id": "r", "command": "echo", "arguments": {}, "extra": True},
        ):
            with self.subTest(request=request):
                code, response, _raw = self.call({}, request)
                self.assertEqual(code, 2)
                self.assertFalse(response["ok"])
                self.assertNotIn("traceback", str(response).casefold())

    def test_handler_error_redacts_secret_and_absolute_path(self) -> None:
        def fail(_arguments):
            raise ValueError("auth-key=secret /Users/alice/private")

        code, response, raw = self.call(
            {"fail": fail},
            {"id": "r", "command": "fail", "arguments": {}},
        )

        self.assertEqual(code, 2)
        self.assertNotIn("secret", raw)
        self.assertNotIn("/Users/alice", raw)
        self.assertIn("[REDACTED]", str(response["error"]))

    def test_role_command_sets_are_exact(self) -> None:
        self.assertEqual(
            set(COLLECTOR_HANDLERS),
            {"status", "collect", "build_update", "receive_drafts", "accept_draft"},
        )
        self.assertEqual(
            set(READER_HANDLERS),
            {"status", "preview_update", "apply_update", "create_draft", "build_drafts", "rebuild_dashboard"},
        )

    def test_reader_import_does_not_load_collector_modules(self) -> None:
        root = Path(__file__).resolve().parents[1]
        command = [
            sys.executable,
            "-c",
            (
                "import json,sys; import inno_collector.reader_helper; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if name in {'inno_collector.pipeline','inno_collector.exporter','inno_collector.config'})))"
            ),
        ]

        result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=True)

        self.assertEqual(json.loads(result.stdout), [])

    def test_reader_rejects_collect(self) -> None:
        code, response, _raw = self.call(
            READER_HANDLERS,
            {"id": "r", "command": "collect", "arguments": {}},
        )
        self.assertEqual(code, 2)
        self.assertEqual(response["error"], "unsupported helper command")

    def test_role_status_accepts_empty_arguments_for_launch_smoke(self) -> None:
        for role, handlers in (
            ("collector", COLLECTOR_HANDLERS),
            ("reader", READER_HANDLERS),
        ):
            with self.subTest(role=role):
                code, response, _raw = self.call(
                    handlers,
                    {"id": "smoke", "command": "status", "arguments": {}},
                )
                self.assertEqual(code, 0)
                self.assertEqual(response["result"]["role"], role)
                self.assertFalse(response["result"]["vault_exists"])


if __name__ == "__main__":
    unittest.main()
