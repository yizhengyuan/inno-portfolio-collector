from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inno_collector.state import CatalogStateStore, ManifestStore


class ManifestStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "nested" / "manifest.json"

    def test_missing_manifest_starts_with_default_data(self) -> None:
        store = ManifestStore(self.path)

        self.assertEqual(store.data, {"version": 1, "articles": {}})
        self.assertEqual(store.load(), {"version": 1, "articles": {}})
        self.assertIsNone(store.get("sha256:missing"))

    def test_upsert_and_get_use_copies(self) -> None:
        store = ManifestStore(self.path)
        article = {
            "title": "原始标题",
            "count": 1,
            "metadata": {"tags": ["original"]},
        }

        store.upsert("sha256:key", article)
        self.assertEqual(
            store.data["articles"]["sha256:key"],
            {
                "title": "原始标题",
                "count": 1,
                "metadata": {"tags": ["original"]},
            },
        )
        article["title"] = "mutated input"
        article["metadata"]["tags"].append("mutated input")
        fetched = store.get("sha256:key")
        self.assertEqual(
            fetched,
            {
                "title": "原始标题",
                "count": 1,
                "metadata": {"tags": ["original"]},
            },
        )

        assert fetched is not None
        fetched["title"] = "mutated output"
        fetched["metadata"]["tags"].append("mutated output")
        self.assertEqual(
            store.get("sha256:key"),
            {
                "title": "原始标题",
                "count": 1,
                "metadata": {"tags": ["original"]},
            },
        )

        store.data["articles"]["sha256:public"] = {"title": "public data"}
        self.assertEqual(store.get("sha256:public"), {"title": "public data"})

    def test_save_reload_preserves_unicode_and_is_idempotent(self) -> None:
        store = ManifestStore(self.path)
        store.data = {
            "version": 1,
            "articles": {
                "sha256:key": {"title": "中文标题", "status": "已采集"}
            },
        }

        store.save()
        first_bytes = self.path.read_bytes()
        reloaded = ManifestStore(self.path)
        self.assertEqual(
            reloaded.get("sha256:key"),
            {"title": "中文标题", "status": "已采集"},
        )
        self.assertIn("中文标题", first_bytes.decode("utf-8"))
        self.assertTrue(first_bytes.endswith(b"\n"))

        reloaded.save()
        self.assertEqual(self.path.read_bytes(), first_bytes)

    def test_save_uses_unique_same_directory_temporary_files(self) -> None:
        store = ManifestStore(self.path)
        store.upsert("sha256:key", {"title": "value"})

        with patch("inno_collector.state.os.replace", wraps=os.replace) as replace:
            store.save()
            store.upsert("sha256:second", {"title": "second"})
            store.save()

        self.assertEqual(replace.call_count, 2)
        temporary_paths = [Path(call.args[0]) for call in replace.call_args_list]
        target_paths = [Path(call.args[1]) for call in replace.call_args_list]
        self.assertEqual(target_paths, [self.path, self.path])
        self.assertEqual(len(set(temporary_paths)), 2)
        self.assertTrue(
            all(temporary.parent == self.path.parent for temporary in temporary_paths)
        )
        self.assertTrue(
            all(
                temporary.name.startswith(self.path.name + ".")
                and temporary.name.endswith(".tmp")
                for temporary in temporary_paths
            )
        )

    def test_rejects_unsupported_manifest_shapes(self) -> None:
        invalid_payloads = (
            {"version": 2, "articles": {}},
            {"version": True, "articles": {}},
            {"version": 1.0, "articles": {}},
            {"version": 1, "articles": []},
            {"version": 1, "articles": {"sha256:key": []}},
            [],
        )
        self.path.parent.mkdir(parents=True)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError, "^unsupported manifest format$"
                ):
                    ManifestStore(self.path)

    def test_malformed_json_raises_stable_manifest_error(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"version": 1,', encoding="utf-8")

        with self.assertRaises(ValueError) as raised:
            ManifestStore(self.path)

        self.assertIs(type(raised.exception), ValueError)
        self.assertEqual(str(raised.exception), "unsupported manifest format")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_invalid_utf8_raises_stable_manifest_error(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff")

        with self.assertRaises(ValueError) as raised:
            ManifestStore(self.path)

        self.assertIs(type(raised.exception), ValueError)
        self.assertEqual(str(raised.exception), "unsupported manifest format")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_load_refreshes_public_data_from_disk(self) -> None:
        store = ManifestStore(self.path)
        payload = {
            "version": 1,
            "articles": {"sha256:new": {"title": "written externally"}},
        }
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(store.load(), payload)
        self.assertEqual(store.data, payload)
        self.assertEqual(store.get("sha256:new"), {"title": "written externally"})

    def test_save_rejects_invalid_public_data_without_overwriting_file(self) -> None:
        store = ManifestStore(self.path)
        store.upsert("sha256:valid", {"title": "preserve me"})
        store.save()
        original = self.path.read_bytes()
        invalid_payloads = (
            {"version": True, "articles": {}},
            {"version": 1, "articles": {2: {}}},
            {"version": 1, "articles": {"sha256:key": []}},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                store.data = payload
                with self.assertRaisesRegex(
                    ValueError, "^unsupported manifest format$"
                ):
                    store.save()

        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_stores_merge_unrelated_article_updates(self) -> None:
        first = ManifestStore(self.path)
        second = ManifestStore(self.path)
        first.upsert("sha256:first", {"title": "first"})
        second.upsert("sha256:second", {"title": "second"})

        first.save()
        second.save()

        expected = {
            "sha256:first": {"title": "first"},
            "sha256:second": {"title": "second"},
        }
        self.assertEqual(ManifestStore(self.path).data["articles"], expected)
        self.assertEqual(second.data["articles"], expected)

    def test_stale_store_does_not_overwrite_record_it_did_not_change(self) -> None:
        seed = ManifestStore(self.path)
        seed.upsert("sha256:existing", {"title": "v0"})
        seed.save()
        first = ManifestStore(self.path)
        second = ManifestStore(self.path)

        first.upsert("sha256:existing", {"title": "v1"})
        first.save()
        second.upsert("sha256:new", {"title": "new"})
        second.save()

        self.assertEqual(
            ManifestStore(self.path).data["articles"],
            {
                "sha256:existing": {"title": "v1"},
                "sha256:new": {"title": "new"},
            },
        )

    def test_stale_store_does_not_restore_record_deleted_by_another_store(self) -> None:
        seed = ManifestStore(self.path)
        seed.upsert("sha256:deleted", {"title": "delete me"})
        seed.save()
        first = ManifestStore(self.path)
        second = ManifestStore(self.path)

        del first.data["articles"]["sha256:deleted"]
        first.save()
        second.upsert("sha256:new", {"title": "new"})
        second.save()

        self.assertEqual(
            ManifestStore(self.path).data["articles"],
            {"sha256:new": {"title": "new"}},
        )

    def test_save_takes_and_releases_an_exclusive_file_lock(self) -> None:
        store = ManifestStore(self.path)
        store.upsert("sha256:key", {"title": "value"})

        with patch("inno_collector.state.fcntl.flock", wraps=fcntl.flock) as flock:
            store.save()

        operations = [call.args[1] for call in flock.call_args_list]
        self.assertEqual(operations, [fcntl.LOCK_EX, fcntl.LOCK_UN])

    def test_replace_failure_preserves_target_and_removes_temporary_file(self) -> None:
        store = ManifestStore(self.path)
        store.upsert("sha256:existing", {"title": "existing"})
        store.save()
        original = self.path.read_bytes()
        store.upsert("sha256:new", {"title": "new"})

        with patch("inno_collector.state.os.replace", side_effect=OSError("replace")):
            with self.assertRaisesRegex(OSError, "^replace$"):
                store.save()

        self.assertEqual(self.path.read_bytes(), original)
        remaining_temporary_files = [
            path for path in self.path.parent.iterdir() if path.name.endswith(".tmp")
        ]
        self.assertEqual(remaining_temporary_files, [])


class CatalogStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "state" / "catalog-state.json"

    def test_missing_state_is_read_only_until_success_is_saved(self) -> None:
        store = CatalogStateStore(self.path)

        self.assertIsNone(store.get("sha256:missing"))
        self.assertFalse(self.path.exists())

        store.mark_success("sha256:key", "sha256:" + "a" * 64)
        store.save()

        self.assertEqual(
            CatalogStateStore(self.path).get("sha256:key"),
            "sha256:" + "a" * 64,
        )

    def test_rejects_invalid_catalog_fingerprint_records(self) -> None:
        self.path.parent.mkdir(parents=True)
        invalid_payloads = (
            {"version": 1, "articles": {"sha256:key": {}}},
            {"version": 1, "articles": {"sha256:key": {"fingerprint": "bad"}}},
            {
                "version": 1,
                "articles": {
                    "sha256:key": {
                        "fingerprint": "sha256:" + "a" * 64,
                        "extra": True,
                    }
                },
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError, "^unsupported catalog state format$"
                ):
                    CatalogStateStore(self.path)

    def test_stale_catalog_stores_merge_unrelated_successes(self) -> None:
        first = CatalogStateStore(self.path)
        second = CatalogStateStore(self.path)
        first.mark_success("sha256:first", "sha256:" + "1" * 64)
        second.mark_success("sha256:second", "sha256:" + "2" * 64)

        first.save()
        second.save()

        reloaded = CatalogStateStore(self.path)
        self.assertEqual(reloaded.get("sha256:first"), "sha256:" + "1" * 64)
        self.assertEqual(reloaded.get("sha256:second"), "sha256:" + "2" * 64)

    def test_concurrent_invalid_catalog_state_is_not_overwritten(self) -> None:
        store = CatalogStateStore(self.path)
        self.path.parent.mkdir(parents=True)
        invalid = {
            "version": 1,
            "articles": {"sha256:external": {"fingerprint": "invalid"}},
        }
        self.path.write_text(json.dumps(invalid), encoding="utf-8")
        original = self.path.read_bytes()
        store.mark_success("sha256:new", "sha256:" + "a" * 64)

        with self.assertRaisesRegex(
            ValueError, "^unsupported catalog state format$"
        ):
            store.save()

        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
