from __future__ import annotations

import unittest

from inno_collector.diagnostics import sanitize_diagnostic


class DiagnosticSanitizerTests(unittest.TestCase):
    def test_redacts_secrets_and_local_paths_including_spaces_and_file_urls(self) -> None:
        cases = (
            "missing '/Users/yzy/My Project/config.json' token=top-secret",
            "open file:///Users/yzy/My%20Project/config.json auth-key=auth-secret",
            'failed at "/Volumes/Private Disk/export/index.csv" pass_ticket=ticket-secret',
            r"failed C:\Users\yzy\Private Folder\config.json uin=uin-secret",
        )

        for message in cases:
            with self.subTest(message=message):
                sanitized = sanitize_diagnostic(message)
                self.assertIn("[path]", sanitized)
                self.assertNotIn("yzy", sanitized)
                self.assertNotIn("Private", sanitized)
                self.assertNotIn("secret", sanitized)
                self.assertLessEqual(len(sanitized), 4096)

    def test_unprintable_exception_uses_stable_fallback(self) -> None:
        class BrokenError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("cannot stringify")

        self.assertEqual(sanitize_diagnostic(BrokenError()), "operation failed")


if __name__ == "__main__":
    unittest.main()
