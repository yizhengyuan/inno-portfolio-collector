from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from inno_collector.exporter import ExporterCommandError
from inno_collector.models import ProjectAccount
from inno_collector.web.moore_runtime import MooreRuntime, load_moore_functions


LOGIN_ID = "a" * 32


class FakeMooreFunctions:
    def __init__(self) -> None:
        self.qrcode_path = ""
        self.status_payload: dict = {
            "ok": True,
            "login_id": LOGIN_ID,
            "status": "confirmed",
            "status_code": 1,
            "acct_size": 3,
            "message": "ready",
            "ready_to_complete": True,
            "auth-key": "must-not-leak",
            "local_path": "/Users/private/login.json",
        }
        self.complete_payload: dict = {
            "profile_id": 8,
            "display_name": "collector",
            "expires_at": "2026-07-16T00:00:00+00:00",
            "nickname": "英诺",
            "avatar": "https://example.invalid/avatar.png",
            "auth-key": "must-not-leak",
        }
        self.auth_payload: dict = {
            "ok": True,
            "status": "valid",
            "code": 0,
            "profile": "collector",
            "expires_at": "2026-07-16T00:00:00+00:00",
            "token": "must-not-leak",
        }
        self.account_rows: list[dict] = [{"id": 11, "nickname": "Alpha"}]
        self.search_payloads: dict[object, object] = {}
        self.search_calls: list[tuple[str, int, int, str]] = []
        self.upsert_calls: list[dict] = []
        self.upsert_payload: object = {
            "ok": True,
            "account": {
                "id": 77,
                "nickname": "Official Name",
                "alias": "wx_official",
                "raw_json": "must-not-cross-runtime-boundary",
            },
        }

    def start_qr_login(self, base: Path, base_url: str) -> dict:
        return {
            "ok": True,
            "login_id": LOGIN_ID,
            "qrcode_path": self.qrcode_path,
            "expires_at": "2026-07-12T12:00:00+00:00",
            "base_url": base_url,
            "next_step": "do not expose",
        }

    def qr_login_status(self, base: Path, login_id: str) -> dict:
        return self.status_payload

    def complete_qr_login(self, base: Path, login_id: str, profile: str) -> dict:
        return self.complete_payload

    def auth_check(self, base: Path, profile: str) -> dict:
        return self.auth_payload

    def list_accounts(self, base: Path) -> list[dict]:
        return [dict(row) for row in self.account_rows]

    def search_accounts(
        self,
        base: Path,
        keyword: str,
        begin: int,
        size: int,
        profile: str,
    ) -> dict:
        self.search_calls.append((keyword, begin, size, profile))
        payload = self.search_payloads.get(
            (keyword, begin),
            self.search_payloads.get(keyword) if begin == 0 else None,
        )
        if payload is None:
            payload = {
                "ok": True,
                "keyword": keyword,
                "begin": begin,
                "size": size,
                "count": 0,
                "accounts": [],
                "raw_code": None,
            }
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, dict):
            result = dict(payload)
            accounts = result.get("accounts")
            result.setdefault("keyword", keyword)
            result.setdefault("begin", begin)
            result.setdefault("size", size)
            result.setdefault("count", len(accounts) if isinstance(accounts, list) else 0)
            return result
        return payload  # type: ignore[return-value]

    def upsert_account(self, base: Path, account: dict) -> dict:
        self.upsert_calls.append(dict(account))
        if isinstance(self.upsert_payload, BaseException):
            raise self.upsert_payload
        payload = self.upsert_payload
        if isinstance(payload, dict):
            payload = dict(payload)
            raw_local = payload.get("account")
            if isinstance(raw_local, dict):
                payload["account"] = {"fakeid": account.get("fakeid", ""), **raw_local}
            local = payload.get("account")
            if (
                payload.get("ok") is True
                and isinstance(local, dict)
                and type(local.get("id")) is int
                and local["id"] > 0
                and local.get("fakeid") == account.get("fakeid")
            ):
                self.account_rows.append(
                    {
                        "id": local["id"],
                        "nickname": account.get("nickname", ""),
                        "alias": account.get("alias", ""),
                    }
                )
        return payload  # type: ignore[return-value]

    def sync_account_articles(
        self, base: Path, account_id: int, limit: int, keyword: str, profile: str
    ) -> dict:
        return {
            "ok": True,
            "account_id": account_id,
            "fetched_count": 2,
            "upserted_count": 2,
            "errors": [],
        }

    def list_articles(
        self,
        base: Path,
        account_id: int,
        limit: int,
        keyword: str,
        collection_id: int,
        downloaded: str,
    ) -> list[dict]:
        return [{"id": 21, "account_id": account_id}]

    def download_articles(
        self,
        base: Path,
        article_ids: list[int],
        output_dir: str,
        no_assets: bool,
        account_nickname: str,
    ) -> dict:
        return {"ok": True, "selected_count": len(article_ids)}


class MooreRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime_dir = Path(self.temp.name) / "runtime"
        self.runtime_dir.mkdir()
        self.functions = FakeMooreFunctions()
        self.qrcode = self.runtime_dir / "login" / f"{LOGIN_ID}.png"
        self.qrcode.parent.mkdir()
        self.qrcode.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.functions.qrcode_path = str(self.qrcode)
        self.runtime = MooreRuntime(self.runtime_dir, functions=self.functions)

    def test_start_returns_only_opaque_metadata_and_registered_qrcode(self) -> None:
        result = self.runtime.start_login("http://127.0.0.1:3000")

        self.assertEqual(
            result,
            {
                "login_id": LOGIN_ID,
                "expires_at": "2026-07-12T12:00:00+00:00",
                "qrcode_content_type": "image/png",
            },
        )
        self.assertEqual(
            self.runtime.read_qrcode(LOGIN_ID),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
        )

    def test_qrcode_requires_current_registered_session(self) -> None:
        with self.assertRaisesRegex(ExporterCommandError, "login session is unavailable"):
            self.runtime.read_qrcode(LOGIN_ID)

    def test_start_rejects_qrcode_outside_runtime(self) -> None:
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"png")
        self.functions.qrcode_path = str(outside)

        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_start_rejects_qrcode_symlink(self) -> None:
        target = self.runtime_dir / "real.png"
        target.write_bytes(b"png")
        symlink = self.runtime_dir / "linked.png"
        symlink.symlink_to(target)
        self.functions.qrcode_path = str(symlink)

        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_start_rejects_qrcode_below_symlinked_directory(self) -> None:
        real_dir = self.runtime_dir / "real-login"
        real_dir.mkdir()
        target = real_dir / "code.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        linked_dir = self.runtime_dir / "linked-login"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        self.functions.qrcode_path = str(linked_dir / "code.png")

        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_start_rejects_missing_and_oversized_qrcode(self) -> None:
        missing = self.runtime_dir / "missing.png"
        self.functions.qrcode_path = str(missing)
        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

        oversized = self.runtime_dir / "oversized.png"
        oversized.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (2 << 20))
        self.functions.qrcode_path = str(oversized)
        with self.assertRaisesRegex(ExporterCommandError, "invalid QR code file"):
            self.runtime.start_login("http://127.0.0.1:3000")

    def test_status_is_allowlisted_and_drops_secrets_and_paths(self) -> None:
        self.runtime.start_login("http://127.0.0.1:3000")

        result = self.runtime.login_status(LOGIN_ID)

        self.assertEqual(
            result,
            {
                "login_id": LOGIN_ID,
                "status": "confirmed",
                "status_code": 1,
                "acct_size": 3,
                "message": "ready",
                "ready_to_complete": True,
            },
        )
        self.assertNotIn("auth-key", result)
        self.assertNotIn("local_path", result)

    def test_complete_is_allowlisted_and_removes_qrcode(self) -> None:
        self.runtime.start_login("http://127.0.0.1:3000")

        result = self.runtime.complete_login(LOGIN_ID, "collector")

        self.assertEqual(
            result,
            {
                "profile_id": 8,
                "display_name": "collector",
                "expires_at": "2026-07-16T00:00:00+00:00",
                "nickname": "英诺",
                "avatar": "https://example.invalid/avatar.png",
            },
        )
        self.assertNotIn("auth-key", result)
        self.assertFalse(self.qrcode.exists())
        with self.assertRaisesRegex(ExporterCommandError, "login session is unavailable"):
            self.runtime.read_qrcode(LOGIN_ID)

    def test_direct_collection_calls_preserve_validation(self) -> None:
        self.assertEqual(self.runtime.auth_check()["status"], "valid")
        self.assertIs(self.runtime.auth_check()["ok"], True)
        self.assertNotIn("token", self.runtime.auth_check())
        self.assertEqual(self.runtime.accounts()[0]["id"], 11)
        self.assertEqual(self.runtime.sync(11)["upserted_count"], 2)
        self.assertEqual(self.runtime.articles(11)[0]["id"], 21)
        self.assertEqual(
            self.runtime.download([21], self.runtime_dir / "output")["selected_count"],
            1,
        )

    def test_exact_cached_account_does_not_search_or_upsert(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Official Name",
            wechat_id="wx_official",
            aliases=("Official Alias",),
        )

        result = self.runtime.resolve_exact(
            project,
            [{"id": 11, "nickname": " official name ", "alias": "other"}],
        )

        self.assertEqual(result["id"], 11)
        self.assertEqual(self.functions.search_calls, [])
        self.assertEqual(self.functions.upsert_calls, [])

    def test_resolve_exact_is_pure_and_uses_primary_then_identifier_priority(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Official Name",
            wechat_id="wx_official",
            aliases=("Official Alias",),
        )
        rows = [
            {"id": 1, "nickname": "Official Name", "alias": "other"},
            {"id": 2, "nickname": "other", "alias": "wx_official"},
            {"id": 3, "nickname": "Official Alias", "alias": "third"},
        ]

        self.assertEqual(self.runtime.resolve_exact(project, rows)["id"], 1)
        self.assertEqual(self.functions.search_calls, [])
        self.assertEqual(self.functions.upsert_calls, [])

        with self.assertRaisesRegex(ExporterCommandError, "got 0$"):
            self.runtime.resolve_exact(project, [])
        self.assertEqual(self.functions.search_calls, [])

    def test_ensure_searches_by_priority_paginates_and_upserts_allowlisted_fields(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Official Name",
            wechat_id="wx_official",
            aliases=("Official Alias", "OFFICIAL NAME"),
        )
        remote = {
            "fakeid": "remote-1",
            "nickname": "Official Name",
            "alias": "other",
            "avatar_url": "https://example.invalid/avatar.png",
            "description": "official account",
            "article_count": 12,
            "raw_json": "must-not-be-forwarded",
            "auth-key": "must-not-be-forwarded",
            "local_path": "/Users/private/profile.json",
        }
        self.functions.search_payloads = {
            "Official Name": {
                "ok": True,
                "raw_code": "0",
                "accounts": [remote],
            },
        }

        rows = self.runtime.ensure_exact_accounts([project])

        self.assertEqual(self.runtime.resolve_exact(project, rows)["id"], 77)
        self.assertEqual(
            [(keyword, begin) for keyword, begin, _, _ in self.functions.search_calls],
            [("Official Name", 0), ("Official Name", 10)],
        )
        self.assertEqual(
            self.functions.upsert_calls,
            [
                {
                    "fakeid": "remote-1",
                    "nickname": "Official Name",
                    "alias": "other",
                    "avatar_url": "https://example.invalid/avatar.png",
                    "description": "official account",
                    "article_count": 12,
                }
            ],
        )

    def test_primary_name_wins_over_alias_from_the_same_search_result(self) -> None:
        project = ProjectAccount(
            project="RayNeo",
            account="RayNeo雷鸟眼镜",
            aliases=("RayNeo雷鸟创新",),
        )
        self.functions.search_payloads["RayNeo雷鸟眼镜"] = {
            "ok": True,
            "raw_code": None,
            "accounts": [
                {
                    "fakeid": "primary",
                    "nickname": "RayNeo雷鸟眼镜",
                    "alias": "",
                    "avatar_url": "",
                    "description": "",
                    "article_count": 0,
                },
                {
                    "fakeid": "configured-alias",
                    "nickname": "RayNeo雷鸟创新",
                    "alias": "",
                    "avatar_url": "",
                    "description": "",
                    "article_count": 0,
                },
            ],
        }

        rows = self.runtime.ensure_exact_accounts([project])

        self.assertEqual(self.runtime.resolve_exact(project, rows)["id"], 77)
        self.assertEqual(self.functions.upsert_calls[0]["fakeid"], "primary")
        self.assertEqual(
            [(call[0], call[1]) for call in self.functions.search_calls],
            [("RayNeo雷鸟眼镜", 0), ("RayNeo雷鸟眼镜", 10)],
        )

    def test_wechat_id_is_used_only_when_the_primary_name_is_missing(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Former Official Name",
            wechat_id="wx_official",
        )
        self.functions.search_payloads = {
            "Former Official Name": {
                "ok": True,
                "raw_code": 0,
                "accounts": [],
            },
            "wx_official": {
                "ok": True,
                "raw_code": 0,
                "accounts": [
                    {
                        "fakeid": "renamed-account",
                        "nickname": "Current Official Name",
                        "alias": "wx_official",
                        "avatar_url": "",
                        "description": "",
                        "article_count": 0,
                    }
                ],
            },
        }

        rows = self.runtime.ensure_exact_accounts([project])

        self.assertEqual(self.runtime.resolve_exact(project, rows)["id"], 77)
        self.assertEqual(
            [(call[0], call[1]) for call in self.functions.search_calls],
            [
                ("Former Official Name", 0),
                ("wx_official", 0),
                ("wx_official", 10),
            ],
        )

    def test_alias_fallback_searches_all_aliases_and_deduplicates_fakeid(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Missing Primary",
            aliases=("Alias One", "Alias Two"),
        )
        remote = {
            "fakeid": "remote-alias",
            "nickname": "Alias One",
            "alias": "",
            "avatar_url": "",
            "description": "",
            "article_count": 0,
        }
        self.functions.search_payloads = {
            "Missing Primary": {"ok": True, "raw_code": None, "accounts": []},
            "Alias One": {"ok": True, "raw_code": None, "accounts": [remote]},
            "Alias Two": {
                "ok": True,
                "raw_code": None,
                "accounts": [dict(remote)],
            },
        }

        rows = self.runtime.ensure_exact_accounts([project])

        self.assertEqual(self.runtime.resolve_exact(project, rows)["id"], 77)
        self.assertEqual(len(self.functions.upsert_calls), 1)
        self.assertEqual(
            [(call[0], call[1]) for call in self.functions.search_calls],
            [
                ("Missing Primary", 0),
                ("Alias One", 0),
                ("Alias One", 10),
                ("Alias Two", 0),
                ("Alias Two", 10),
            ],
        )

    def test_same_priority_multiple_matches_are_never_auto_selected(self) -> None:
        project = ProjectAccount(project="Project A", account="Official Name")
        cached = [
            {"id": 1, "nickname": "Official Name", "alias": "first"},
            {"id": 2, "nickname": "official name", "alias": "second"},
        ]
        with self.assertRaisesRegex(ExporterCommandError, "got 2$"):
            self.runtime.resolve_exact(project, cached)

        self.functions.search_payloads["Official Name"] = {
            "ok": True,
            "raw_code": 0,
            "accounts": [
                {
                    "fakeid": "remote-1",
                    "nickname": "Official Name",
                    "alias": "first",
                    "avatar_url": "",
                    "description": "",
                    "article_count": 0,
                },
                {
                    "fakeid": "remote-2",
                    "nickname": "Official Name",
                    "alias": "second",
                    "avatar_url": "",
                    "description": "",
                    "article_count": 0,
                },
            ],
        }
        rows = self.runtime.ensure_exact_accounts([project])
        self.assertEqual(self.functions.upsert_calls, [])
        with self.assertRaisesRegex(ExporterCommandError, "^account discovery failed$"):
            self.runtime.resolve_exact(project, rows)

    def test_discovery_rejects_fuzzy_crossed_unsafe_and_nonzero_raw_code(self) -> None:
        cases = (
            (
                "fuzzy",
                {"ok": True, "raw_code": 0, "accounts": [{
                    "fakeid": "fuzzy", "nickname": "Official Name Plus",
                    "alias": "Official Name", "avatar_url": "",
                    "description": "", "article_count": 0,
                }]},
            ),
            (
                "unsafe",
                {"ok": True, "raw_code": 0, "accounts": [{
                    "fakeid": "unsafe", "nickname": "..", "alias": "wx_official",
                    "avatar_url": "", "description": "", "article_count": 0,
                }]},
            ),
            (
                "utf8-name-budget",
                {"ok": True, "raw_code": 0, "accounts": [{
                    "fakeid": "oversized", "nickname": "中" * 100,
                    "alias": "wx_official", "avatar_url": "",
                    "description": "", "article_count": 0,
                }]},
            ),
            ("unauthorized", {"ok": True, "raw_code": 401, "accounts": []}),
        )
        for label, payload in cases:
            with self.subTest(label=label):
                functions = FakeMooreFunctions()
                functions.search_payloads["wx_official"] = payload
                runtime = MooreRuntime(self.runtime_dir, functions=functions)
                project = ProjectAccount(
                    project="Project A",
                    account="Official Name",
                    wechat_id="wx_official",
                )

                rows = runtime.ensure_exact_accounts([project])

                self.assertEqual(functions.upsert_calls, [])
                with self.assertRaisesRegex(
                    ExporterCommandError, "^account discovery failed$"
                ):
                    runtime.resolve_exact(project, rows)

    def test_discovery_rejects_wrong_page_metadata_and_repeated_pages(self) -> None:
        project = ProjectAccount(project="Project A", account="Official Name")
        candidate = {
            "fakeid": "remote-1",
            "nickname": "Official Name",
            "alias": "",
            "avatar_url": "",
            "description": "",
            "article_count": 0,
        }
        invalid_payloads = (
            {"keyword": "wrong", "accounts": []},
            {"begin": 10, "accounts": []},
            {"size": 99, "accounts": []},
            {"count": 2, "accounts": [candidate]},
        )
        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid):
                functions = FakeMooreFunctions()
                functions.search_payloads["Official Name"] = {
                    "ok": True,
                    "raw_code": 0,
                    **invalid,
                }
                runtime = MooreRuntime(self.runtime_dir, functions=functions)

                rows = runtime.ensure_exact_accounts([project])

                self.assertEqual(functions.upsert_calls, [])
                with self.assertRaisesRegex(
                    ExporterCommandError, "^account discovery failed$"
                ):
                    runtime.resolve_exact(project, rows)

        functions = FakeMooreFunctions()
        functions.search_payloads = {
            ("Official Name", 0): {
                "ok": True, "raw_code": 0, "accounts": [candidate]
            },
            ("Official Name", 10): {
                "ok": True, "raw_code": 0, "accounts": [dict(candidate)]
            },
        }
        runtime = MooreRuntime(self.runtime_dir, functions=functions)
        rows = runtime.ensure_exact_accounts([project])
        self.assertEqual(functions.upsert_calls, [])
        with self.assertRaisesRegex(ExporterCommandError, "^account discovery failed$"):
            runtime.resolve_exact(project, rows)

    def test_cached_alias_match_with_unsafe_nickname_is_rejected(self) -> None:
        project = ProjectAccount(
            project="Project A",
            account="Official Name",
            wechat_id="wx_official",
        )

        with self.assertRaisesRegex(ExporterCommandError, "unsafe account name"):
            self.runtime.resolve_exact(
                project,
                [{"id": 7, "nickname": "..", "alias": "wx_official"}],
            )

        self.assertEqual(self.functions.search_calls, [])
        self.assertEqual(self.functions.upsert_calls, [])

    def test_discovery_failures_are_isolated_and_details_never_leak(self) -> None:
        failed = ProjectAccount(project="Failed", account="Broken Account")
        successful = ProjectAccount(project="Successful", account="Good Account")
        self.functions.search_payloads = {
            "Broken Account": RuntimeError(
                "token=super-secret at /Users/private/profile.json"
            ),
            "Good Account": {
                "ok": True,
                "raw_code": 0,
                "accounts": [{
                    "fakeid": "good", "nickname": "Good Account", "alias": "",
                    "avatar_url": "", "description": "", "article_count": 0,
                }],
            },
        }

        rows = self.runtime.ensure_exact_accounts([failed, successful])

        self.assertEqual(self.runtime.resolve_exact(successful, rows)["id"], 77)
        with self.assertRaises(ExporterCommandError) as raised:
            self.runtime.resolve_exact(failed, rows)
        self.assertEqual(str(raised.exception), "account discovery failed")
        self.assertNotIn("super-secret", str(raised.exception))
        self.assertNotIn("/Users", str(raised.exception))

    def test_discovery_requires_positive_upsert_id_and_resets_prior_errors(self) -> None:
        project = ProjectAccount(project="Project A", account="Official Name")
        self.functions.search_payloads["Official Name"] = {
            "ok": True,
            "raw_code": 0,
            "accounts": [{
                "fakeid": "remote-1", "nickname": "Official Name", "alias": "",
                "avatar_url": "", "description": "", "article_count": 0,
            }],
        }
        self.functions.upsert_payload = {"ok": True, "account": {"id": 0}}
        rows = self.runtime.ensure_exact_accounts([project])
        with self.assertRaisesRegex(ExporterCommandError, "^account discovery failed$"):
            self.runtime.resolve_exact(project, rows)

        self.functions.upsert_payload = {
            "ok": True,
            "account": {"id": 77, "fakeid": "different-remote"},
        }
        rows = self.runtime.ensure_exact_accounts([project])
        with self.assertRaisesRegex(ExporterCommandError, "^account discovery failed$"):
            self.runtime.resolve_exact(project, rows)

        self.functions.account_rows.append(
            {"id": 88, "nickname": "Official Name", "alias": ""}
        )
        rows = self.runtime.ensure_exact_accounts([project])
        self.assertEqual(self.runtime.resolve_exact(project, rows)["id"], 88)

    def test_malformed_direct_results_are_rejected(self) -> None:
        self.functions.auth_payload = {"ok": "true", "status": "valid"}
        with self.assertRaisesRegex(ExporterCommandError, "exporter command failed"):
            self.runtime.auth_check()

        self.functions.list_accounts = lambda base: ["not-an-object"]  # type: ignore[method-assign]
        with self.assertRaisesRegex(ExporterCommandError, "invalid accounts"):
            self.runtime.accounts()

    def test_upstream_exception_uses_stable_error_without_secret_or_path(self) -> None:
        def fail(base: Path, profile: str) -> dict:
            raise RuntimeError(f"token=super-secret at {base / 'profile.json'}")

        self.functions.auth_check = fail  # type: ignore[method-assign]

        with self.assertRaises(ExporterCommandError) as raised:
            self.runtime.auth_check()

        self.assertEqual(str(raised.exception), "local exporter operation failed")

    def test_runtime_loader_requires_account_discovery_functions(self) -> None:
        module = ModuleType("wechat_exporter")
        for name in (
            "start_qr_login",
            "qr_login_status",
            "complete_qr_login",
            "auth_check",
            "list_accounts",
            "sync_account_articles",
            "list_articles",
            "download_articles",
        ):
            setattr(module, name, lambda *args: None)

        with patch("inno_collector.web.moore_runtime.importlib.import_module", return_value=module):
            with self.assertRaisesRegex(
                ExporterCommandError, "^local exporter runtime is incompatible$"
            ):
                load_moore_functions()


if __name__ == "__main__":
    unittest.main()
