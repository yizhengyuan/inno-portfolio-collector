from __future__ import annotations

import http.client
import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FROZEN_SERVER = os.environ.get("INNO_COLLECTOR_WEB_SERVER")
FROZEN_READY_TIMEOUT_SECONDS = 90


@unittest.skipUnless(
    FROZEN_SERVER,
    "requires INNO_COLLECTOR_WEB_SERVER",
)
class FrozenWebServerTests(unittest.TestCase):
    def test_cold_start_ready_http_resources_and_clean_exit(self) -> None:
        assert FROZEN_SERVER is not None
        binary = Path(FROZEN_SERVER).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="inno-frozen-web-") as temporary:
            root = Path(temporary)
            home = root / "home"
            temporary_runtime = root / "tmp"
            support_root = root / "support"
            resources = root / "Collector.app/Contents/Resources/config"
            for directory in (home, temporary_runtime, resources):
                directory.mkdir(parents=True)
            projects = resources / "projects.json"
            shutil.copyfile(
                ROOT / "src/inno_collector/web/resources/projects.json",
                projects,
            )
            process = subprocess.Popen(
                [
                    str(binary),
                    "--support-root",
                    str(support_root),
                    "--projects",
                    str(projects),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "HOME": str(home),
                    "TMPDIR": str(temporary_runtime),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "en_US.UTF-8",
                },
            )
            self.addCleanup(self._cleanup_process, process)
            port: int | None = None
            try:
                assert process.stdout is not None
                # Match the packaged launcher allowance: a first PyInstaller
                # run may spend 20–30 seconds unpacking on a slower Mac, but
                # the handshake must still arrive within a bounded window.
                readable, _, _ = select.select(
                    [process.stdout],
                    [],
                    [],
                    FROZEN_READY_TIMEOUT_SECONDS,
                )
                self.assertTrue(readable, "frozen server ready handshake timed out")
                ready_line = process.stdout.readline(4097)
                self.assertLessEqual(len(ready_line), 4096)
                self.assertTrue(ready_line.endswith(b"\n"))
                self.assertEqual(ready_line.count(b"\n"), 1)
                ready = json.loads(ready_line)
                self.assertEqual(
                    set(ready),
                    {"protocol", "host", "port", "pid"},
                )
                self.assertEqual(ready["protocol"], 1)
                self.assertEqual(ready["host"], "127.0.0.1")
                self.assertEqual(ready["pid"], process.pid)
                self.assertIs(type(ready["port"]), int)
                self.assertGreaterEqual(ready["port"], 1)
                self.assertLessEqual(ready["port"], 65535)
                port = ready["port"]

                for path in (
                    "/health",
                    "/",
                    "/assets/app.css",
                    "/assets/app.js",
                    "/api/bootstrap",
                ):
                    with self.subTest(path=path):
                        status, headers, body = self._request(port, path)
                        self.assertEqual(status, 200)
                        self.assertTrue(body)
                        self.assertIn("no-store", headers["Cache-Control"])
                        if path == "/api/bootstrap":
                            payload = json.loads(body)
                            self.assertIs(payload["authenticated"], False)
                            self.assertTrue(
                                {
                                    "login",
                                    "preflight",
                                    "collection",
                                    "delivery",
                                    "drafts",
                                }.issubset(set(payload["capabilities"]))
                            )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

            self.assertIn(process.returncode, (-15, 0))
            assert process.stdout is not None
            assert process.stderr is not None
            self.assertEqual(process.stdout.read(), b"")
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            self.assertNotIn(b"/Users/", stderr)
            self.assertNotIn(b"/Volumes/", stderr)
            self.assertNotIn(b"token", stderr.lower())
            self.assertIsNotNone(port)
            self._assert_port_closed(port)

    @staticmethod
    def _request(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "GET",
                path,
                headers={"Host": f"127.0.0.1:{port}"},
            )
            response = connection.getresponse()
            return (
                response.status,
                dict(response.getheaders()),
                response.read(),
            )
        finally:
            connection.close()

    def _assert_port_closed(self, port: int) -> None:
        deadline = time.monotonic() + 2
        while True:
            try:
                connection = socket.create_connection(
                    ("127.0.0.1", port),
                    timeout=0.2,
                )
            except OSError:
                return
            else:
                connection.close()
            if time.monotonic() >= deadline:
                self.fail("frozen server port remained open after exit")
            time.sleep(0.05)

    @staticmethod
    def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
