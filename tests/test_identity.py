from __future__ import annotations

import hashlib
import unittest
from datetime import date
from typing import get_type_hints

from inno_collector.identity import (
    _published_date,
    article_key,
    canonical_url,
    select_since,
)


class CanonicalUrlTests(unittest.TestCase):
    def test_short_links_ignore_secret_and_tracking_query_values(self) -> None:
        first = canonical_url(
            " https://mp.weixin.qq.com/s/article-slug?scene=1&pass_ticket=secret "
        )
        second = canonical_url(
            "http://mp.weixin.qq.com/s/article-slug?scene=99&pass_ticket=other"
        )

        self.assertEqual(first, "https://mp.weixin.qq.com/s/article-slug")
        self.assertEqual(first, second)
        self.assertNotIn("secret", first)

    def test_legacy_links_keep_only_sorted_stable_fields(self) -> None:
        first = canonical_url(
            "https://mp.weixin.qq.com/s?sn=abc&token=secret&idx=2&mid=10&__biz=wx"
        )
        second = canonical_url(
            "http://mp.weixin.qq.com/s/?mid=10&__biz=wx&sn=abc&idx=2&token=other"
        )

        expected = "https://mp.weixin.qq.com/s?__biz=wx&idx=2&mid=10&sn=abc"
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertNotIn("token", first)

    def test_rejects_non_article_paths_and_invalid_legacy_queries(self) -> None:
        invalid_values = (
            "https://mp.weixin.qq.com/",
            "https://mp.weixin.qq.com/s",
            "https://mp.weixin.qq.com/mp/appmsgalbum?token=x#part",
            "https://mp.weixin.qq.com/other/path",
            "https://mp.weixin.qq.com/s/a/b",
            "https://mp.weixin.qq.com/s?__biz=wx&mid=1&idx=2",
            "https://mp.weixin.qq.com/s?__biz=wx&mid=1&idx=2&sn=",
            "https://mp.weixin.qq.com/s?__biz=wx&mid=1&idx=2&sn=a&sn=b",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "^unsupported article URL$"
                ):
                    canonical_url(value)

    def test_rejects_userinfo_and_non_default_ports(self) -> None:
        invalid_values = (
            "https://user@mp.weixin.qq.com/s/article",
            "https://mp.weixin.qq.com:444/s/article",
            "http://mp.weixin.qq.com:8080/s/article",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "^unsupported article URL$"
                ):
                    canonical_url(value)

    def test_accepts_default_ports_and_removes_them(self) -> None:
        expected = "https://mp.weixin.qq.com/s/article"

        self.assertEqual(canonical_url("http://mp.weixin.qq.com:80/s/article"), expected)
        self.assertEqual(
            canonical_url("https://mp.weixin.qq.com:443/s/article"), expected
        )

    def test_rejects_non_wechat_hosts_and_unsupported_schemes(self) -> None:
        invalid_values = (
            "https://example.com/s/example",
            "https://mp.weixin.qq.com.evil.test/s/example",
            "ftp://mp.weixin.qq.com/s/example",
            "not a URL",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "^unsupported article URL$"
                ):
                    canonical_url(value)

    def test_article_key_hashes_the_canonical_url(self) -> None:
        canonical = "https://mp.weixin.qq.com/s/article-slug"
        expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        self.assertEqual(
            article_key(canonical + "?pass_ticket=secret"),
            expected,
        )


class SelectSinceTests(unittest.TestCase):
    def test_published_date_stringifies_date_values(self) -> None:
        self.assertEqual(_published_date(date(2025, 1, 2)), date(2025, 1, 2))

    def test_published_date_accepts_iso_datetime_and_date_prefix(self) -> None:
        self.assertEqual(str(_published_date("2025-03-04T12:30:00Z")), "2025-03-04")
        self.assertEqual(str(_published_date("2025-03-04 extra")), "2025-03-04")
        self.assertIsNone(_published_date(""))
        self.assertIsNone(_published_date("not-a-date"))

    def test_since_includes_cutoff_day_and_excludes_one_second_before(self) -> None:
        before = {
            "id": 1,
            "publish_time": "2024-12-31T23:59:59+08:00",
            "url": "https://mp.weixin.qq.com/s/before",
        }
        cutoff = {
            "id": 2,
            "publish_time": "2025-01-01T00:00:00+08:00",
            "url": "https://mp.weixin.qq.com/s/cutoff",
        }

        self.assertEqual(select_since([before, cutoff], "2025-01-01"), [cutoff])

    def test_deduplicates_by_article_identity_and_keeps_first_row(self) -> None:
        first = {
            "id": 7,
            "publish_time": "2025-02-01T08:00:00+08:00",
            "url": "https://mp.weixin.qq.com/s/same?scene=1",
            "title": "first",
        }
        duplicate = {
            "id": 8,
            "publish_time": "2025-02-02T08:00:00+08:00",
            "url": "https://mp.weixin.qq.com/s/same?pass_ticket=secret",
            "title": "duplicate",
        }

        self.assertEqual(select_since([first, duplicate], "2025-01-01"), [first])

    def test_excludes_missing_or_invalid_dates_and_missing_urls(self) -> None:
        valid = {
            "id": "10",
            "publish_time": "2025-02-02",
            "url": "https://mp.weixin.qq.com/s/valid",
        }
        rows = [
            {"id": 1, "url": "https://mp.weixin.qq.com/s/no-date"},
            {
                "id": 2,
                "publish_time": "invalid",
                "url": "https://mp.weixin.qq.com/s/bad-date",
            },
            {"id": 3, "publish_time": "2025-02-03"},
            valid,
        ]

        self.assertEqual(select_since(rows, "2025-01-01"), [valid])

    def test_skips_bad_article_urls_without_aborting_the_batch(self) -> None:
        invalid = {
            "id": 2,
            "publish_time": "2025-02-02",
            "url": "https://example.com/s/not-wechat",
        }
        valid = {
            "id": 1,
            "publish_time": "2025-02-01",
            "url": "https://mp.weixin.qq.com/s/valid-row",
        }

        self.assertEqual(select_since([invalid, valid], "2025-01-01"), [valid])

    def test_sorts_newest_publish_time_then_numeric_id(self) -> None:
        rows = [
            {
                "id": "2",
                "publish_time": "2025-02-01",
                "url": "https://mp.weixin.qq.com/s/two",
            },
            {
                "id": "10",
                "publish_time": "2025-02-01",
                "url": "https://mp.weixin.qq.com/s/ten",
            },
            {
                "id": "1",
                "publish_time": "2025-02-02",
                "url": "https://mp.weixin.qq.com/s/newest",
            },
        ]

        self.assertEqual(
            [row["id"] for row in select_since(rows, "2025-01-01")],
            ["1", "10", "2"],
        )

    def test_sorts_mixed_times_in_utc_and_normalizes_non_numeric_ids(self) -> None:
        rows = [
            {
                "id": "not-a-number",
                "publish_time": "2026-01-02",
                "url": "https://mp.weixin.qq.com/s/midnight-bad-id",
            },
            {
                "id": "5",
                "publish_time": "2026-01-02",
                "url": "https://mp.weixin.qq.com/s/midnight-id-five",
            },
            {
                "id": "99",
                "publish_time": "2026-01-02T01:00:00+02:00",
                "url": "https://mp.weixin.qq.com/s/previous-utc-day",
            },
            {
                "id": "also-not-a-number",
                "publish_time": "2026-01-01T20:00:00-05:00",
                "url": "https://mp.weixin.qq.com/s/newest-utc",
            },
        ]

        self.assertEqual(
            [row["url"].rsplit("/", 1)[-1] for row in select_since(rows, "2026-01-01")],
            [
                "newest-utc",
                "midnight-id-five",
                "midnight-bad-id",
                "previous-utc-day",
            ],
        )

    def test_overflowing_numeric_id_is_normalized_without_aborting(self) -> None:
        row = {
            "id": float("inf"),
            "publish_time": "2026-01-02",
            "url": "https://mp.weixin.qq.com/s/overflowing-id",
        }

        self.assertEqual(select_since([row], "2026-01-01"), [row])

    def test_stringifies_url_values_and_exposes_list_of_dict_contract(self) -> None:
        class ArticleUrl:
            def __str__(self) -> str:
                return "https://mp.weixin.qq.com/s/stringifiable"

        row = {
            "id": 1,
            "publish_time": "2025-02-01",
            "url": ArticleUrl(),
        }

        self.assertEqual(select_since([row], "2025-01-01"), [row])
        self.assertEqual(
            get_type_hints(select_since),
            {"rows": list[dict], "since": str, "return": list[dict]},
        )


if __name__ == "__main__":
    unittest.main()
