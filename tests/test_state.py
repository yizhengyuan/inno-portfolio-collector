from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inno_collector.state import ManifestStore


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
        article = {"title": "原始标题", "count": 1}

        store.upsert("sha256:key", article)
        self.assertEqual(
            store.data["articles"]["sha256:key"],
            {"title": "原始标题", "count": 1},
        )
        article["title"] = "mutated input"
        fetched = store.get("sha256:key")
        self.assertEqual(fetched, {"title": "原始标题", "count": 1})

        assert fetched is not None
        fetched["title"] = "mutated output"
        self.assertEqual(
            store.get("sha256:key"), {"title": "原始标题", "count": 1}
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

    def test_save_atomically_replaces_target_from_same_directory(self) -> None:
        store = ManifestStore(self.path)
        store.upsert("sha256:key", {"title": "value"})

        with patch("inno_collector.state.os.replace") as replace:
            store.save()

        replace.assert_called_once()
        temporary, target = map(Path, replace.call_args.args)
        self.assertEqual(target, self.path)
        self.assertEqual(temporary, self.path.with_suffix(".json.tmp"))
        self.assertEqual(temporary.parent, target.parent)
        payload = json.loads(temporary.read_text(encoding="utf-8"))
        self.assertEqual(payload["articles"]["sha256:key"]["title"], "value")

    def test_rejects_unsupported_manifest_shapes(self) -> None:
        invalid_payloads = (
            {"version": 2, "articles": {}},
            {"version": 1, "articles": []},
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


if __name__ == "__main__":
    unittest.main()
