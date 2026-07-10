from __future__ import annotations

import hashlib
import unittest

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

    def test_other_paths_drop_query_and_fragment(self) -> None:
        self.assertEqual(
            canonical_url("https://mp.weixin.qq.com/mp/appmsgalbum?token=x#part"),
            "https://mp.weixin.qq.com/mp/appmsgalbum",
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


if __name__ == "__main__":
    unittest.main()
