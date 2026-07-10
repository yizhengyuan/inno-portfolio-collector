from __future__ import annotations

import codecs
import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from inno_collector import ingest as ingest_module
from inno_collector.identity import article_key
from inno_collector.ingest import ingest_account_output, yaml_string
from inno_collector.models import ProjectAccount


FIXTURES = Path(__file__).parent / "fixtures"
FIELDS = (
    "title",
    "publish_time",
    "source_url",
    "markdown_path",
    "image_dir",
    "status",
)
LONG_BODY = "# 正文\n\n" + "这是一段用于验证文章采集与正文规范化的中文内容。" * 12


class IngestAccountOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = ProjectAccount(project="项目甲", account="创新观察")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_index(
        self,
        rows: list[dict[str, str]],
        *,
        encoding: str = "utf-8",
    ) -> None:
        with (self.root / "index.csv").open("w", encoding=encoding, newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def write_article(self, name: str = "article.md", body: str = LONG_BODY) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def row(self, **updates: str) -> dict[str, str]:
        row = {
            "title": "一篇有效文章",
            "publish_time": "2026-04-03T12:30:00+08:00",
            "source_url": "https://mp.weixin.qq.com/s/valid-article",
            "markdown_path": "article.md",
            "image_dir": "",
            "status": "success",
        }
        row.update(updates)
        return row

    def assert_format_error(self, expected: str) -> Exception:
        with self.assertRaises(ValueError) as raised:
            ingest_account_output(self.project, self.root)
        self.assertEqual(type(raised.exception).__name__, "IngestFormatError")
        self.assertEqual(str(raised.exception), expected)
        self.assertIsNone(raised.exception.__cause__)
        return raised.exception

    def test_missing_or_non_file_index_has_stable_format_error(self) -> None:
        self.assertTrue(hasattr(ingest_module, "IngestFormatError"))
        self.assert_format_error("missing exporter index")

        (self.root / "index.csv").mkdir()
        self.assert_format_error("missing exporter index")

    def test_rejects_all_structurally_invalid_exporter_indexes(self) -> None:
        header = ",".join(FIELDS)
        values = ",".join(self.row()[field] for field in FIELDS)
        malformed_indexes = {
            "empty": b"",
            "header only": header.encode("utf-8"),
            "no header": values.encode("utf-8"),
            "missing required header": (
                "title,publish_time,source_url,markdown_path,image_dir\n"
                + ",".join(self.row()[field] for field in FIELDS[:-1])
            ).encode("utf-8"),
            "duplicate required header": (
                "title,publish_time,source_url,markdown_path,title,status\n" + values
            ).encode("utf-8"),
            "duplicate header row as data": f"{header}\n{header}\n".encode("utf-8"),
            "surplus unnamed cell": f"{header}\n{values},extra\n".encode("utf-8"),
            "short row": f"{header}\n{','.join(values.split(',')[:-1])}\n".encode(
                "utf-8"
            ),
            "non utf8": b"\xff\xfe\x80",
            "malformed csv": f'{header}\n"unterminated'.encode("utf-8"),
        }

        for name, payload in malformed_indexes.items():
            with self.subTest(name=name):
                (self.root / "index.csv").write_bytes(payload)
                self.assert_format_error("invalid exporter index")

    def test_allows_extra_named_exporter_columns(self) -> None:
        self.write_article()
        header = ",".join((*FIELDS, "exporter_id"))
        values = ",".join((*[self.row()[field] for field in FIELDS], "42"))
        (self.root / "index.csv").write_text(
            f"{header}\n{values}\n", encoding="utf-8"
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(len(result.valid), 1)
        self.assertEqual(result.rejected, ())

    def test_ingests_exporter_fixture_into_task2_model(self) -> None:
        shutil.copy(FIXTURES / "exporter-article.md", self.root)
        shutil.copy(FIXTURES / "exporter-index.csv", self.root / "index.csv")

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.valid), 1)
        article = result.valid[0]
        expected_url = "https://mp.weixin.qq.com/s/exporter-fixture"
        self.assertEqual(article.key, article_key(expected_url))
        self.assertEqual(article.project, "项目甲")
        self.assertEqual(article.account, "创新观察")
        self.assertEqual(article.published, "2026-05-18")
        self.assertEqual(article.source_url, expected_url)
        self.assertNotIn("pass_ticket", article.source_url)
        self.assertEqual(article.source_markdown, (self.root / "exporter-article.md").resolve())
        self.assertIsNone(article.source_image_dir)
        self.assertTrue(article.body.endswith("\n"))
        self.assertFalse(article.body.endswith("\n\n"))
        self.assertEqual(
            article.content_hash,
            "sha256:" + hashlib.sha256(article.body.encode("utf-8")).hexdigest(),
        )
        collected_at = datetime.fromisoformat(article.collected_at)
        self.assertIsNotNone(collected_at.tzinfo)
        self.assertEqual(collected_at.microsecond, 0)

    def test_rejects_short_login_prompt_without_losing_valid_row(self) -> None:
        self.write_article()
        self.write_article("login.md", "请登录后扫码登录，输入验证码继续。")
        self.write_index(
            [
                self.row(),
                self.row(
                    title="登录提示",
                    source_url="https://mp.weixin.qq.com/s/login-prompt",
                    markdown_path="login.md",
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual([article.title for article in result.valid], ["一篇有效文章"])
        self.assertEqual(
            [(article.title, article.reason) for article in result.rejected],
            [("登录提示", "invalid_body")],
        )

    def test_preserves_safe_existing_image_directory(self) -> None:
        self.write_article()
        image_dir = self.root / "images" / "文章"
        image_dir.mkdir(parents=True)
        (image_dir / "001.jpg").write_bytes(b"image")
        self.write_index([self.row(image_dir="images/文章")])

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(result.rejected, ())
        self.assertEqual(result.valid[0].source_image_dir, image_dir.resolve())

    def test_symlinked_images_root_is_never_accepted(self) -> None:
        self.write_article()
        with tempfile.TemporaryDirectory() as external_directory:
            external_images = Path(external_directory)
            (external_images / "article-assets").mkdir()
            (self.root / "images").symlink_to(
                external_images, target_is_directory=True
            )
            self.write_index([self.row(image_dir="images/article-assets")])

            result = ingest_account_output(self.project, self.root)

        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.valid), 1)
        self.assertIsNone(result.valid[0].source_image_dir)

    def test_unsafe_or_unavailable_image_directories_do_not_reject_articles(self) -> None:
        self.write_article()
        images_root = self.root / "images"
        images_root.mkdir()
        not_a_directory = images_root / "image-file"
        not_a_directory.write_bytes(b"image")
        articles_directory = self.root / "articles"
        articles_directory.mkdir()
        (images_root / "articles-link").symlink_to(
            articles_directory, target_is_directory=True
        )
        (images_root / "root-link").symlink_to(self.root, target_is_directory=True)
        image_dirs = (
            ".",
            "images/..",
            "images/.",
            "images/articles-link",
            "images/root-link",
            "../outside-images",
            "images/missing-images",
            "images/image-file",
            str(images_root),
            "C:\\outside\\images",
        )
        self.write_index(
            [
                self.row(
                    title=f"图片目录样本{index}",
                    source_url=f"https://mp.weixin.qq.com/s/image-dir-{index}",
                    image_dir=image_dir,
                )
                for index, image_dir in enumerate(image_dirs, start=1)
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.valid), len(image_dirs))
        self.assertTrue(all(article.source_image_dir is None for article in result.valid))

    def test_normalizes_crlf_and_cr_before_hashing(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        lf_body = "# 标题\n\n" + "正文内容足够长，用于确认不同换行格式产生相同摘要。\n" * 10
        crlf_body = lf_body.replace("\n", "\r\n")
        (first / "article.md").write_bytes(lf_body.encode("utf-8"))
        (second / "article.md").write_bytes(crlf_body.encode("utf-8"))
        row = self.row()
        for directory in (first, second):
            with (directory / "index.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow(row)

        first_article = ingest_account_output(self.project, first).valid[0]
        second_article = ingest_account_output(self.project, second).valid[0]

        self.assertNotIn("\r", second_article.body)
        self.assertEqual(first_article.body, second_article.body)
        self.assertEqual(first_article.content_hash, second_article.content_hash)

    def test_non_success_status_is_download_failed_before_other_validation(self) -> None:
        self.write_index(
            [
                self.row(
                    title="",
                    publish_time="bad",
                    source_url="not a url",
                    markdown_path="missing.md",
                    status=" FAILED ",
                )
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(len(result.valid), 0)
        self.assertEqual(result.rejected[0].reason, "download_failed")
        self.assertIsInstance(result.rejected[0].title, str)
        self.assertIsInstance(result.rejected[0].source_url, str)

    def test_missing_or_non_file_markdown_is_missing_file(self) -> None:
        (self.root / "directory.md").mkdir()
        self.write_index(
            [
                self.row(markdown_path="missing.md"),
                self.row(
                    title="目录",
                    source_url="https://mp.weixin.qq.com/s/directory",
                    markdown_path="directory.md",
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(
            [article.reason for article in result.rejected],
            ["missing_file", "missing_file"],
        )

    def test_rejects_parent_traversal_and_absolute_markdown_paths(self) -> None:
        absolute = self.root / "absolute.md"
        absolute.write_text(LONG_BODY, encoding="utf-8")
        self.write_index(
            [
                self.row(markdown_path="../outside.md"),
                self.row(
                    title="绝对路径",
                    source_url="https://mp.weixin.qq.com/s/absolute",
                    markdown_path=str(absolute),
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(
            [article.reason for article in result.rejected],
            ["invalid_path", "invalid_path"],
        )

    def test_rejects_invalid_url_and_invalid_publish_date_independently(self) -> None:
        self.write_article()
        self.write_index(
            [
                self.row(source_url="https://example.com/not-wechat"),
                self.row(
                    title="坏日期",
                    publish_time="2026-02-30",
                    source_url="https://mp.weixin.qq.com/s/bad-date",
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(
            [article.reason for article in result.rejected],
            ["invalid_url", "invalid_metadata"],
        )

    def test_rejects_empty_title_and_too_short_body(self) -> None:
        self.write_article("short.md", "# 短文\n内容不足。")
        self.write_index(
            [
                self.row(title=""),
                self.row(
                    title="短正文",
                    source_url="https://mp.weixin.qq.com/s/short-body",
                    markdown_path="short.md",
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(
            [article.reason for article in result.rejected],
            ["invalid_metadata", "invalid_body"],
        )

    def test_long_article_discussing_login_is_not_mistaken_for_prompt(self) -> None:
        body = "# 登录系统设计复盘\n\n" + (
            "本文讨论用户登录、扫码登录和验证码流程的设计取舍，并记录完整技术分析。"
            * 30
        )
        self.write_article(body=body)
        self.write_index([self.row(title="登录系统设计复盘")])

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(len(result.valid), 1)
        self.assertEqual(result.rejected, ())

    def test_medium_complete_article_with_single_login_mentions_is_valid(self) -> None:
        body = (
            "# 产品说明\n\n"
            + "本文完整介绍产品背景、使用步骤、数据处理方法与常见问题。" * 6
            + "首次使用时可以扫码登录，并通过验证码确认身份。"
            + "完成认证后即可阅读后续章节和操作示例。" * 3
        )
        compact_length = sum(not character.isspace() for character in body)
        self.assertGreaterEqual(compact_length, 100)
        self.assertLessEqual(compact_length, 300)
        self.write_article(body=body)
        self.write_index([self.row(title="包含登录说明的完整文章")])

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(len(result.valid), 1)
        self.assertEqual(result.rejected, ())

    def test_repeated_login_prompt_over_500_characters_is_invalid(self) -> None:
        body = "请登录后扫码登录并输入验证码继续访问。" * 60
        self.assertGreater(sum(not character.isspace() for character in body), 500)
        self.write_article(body=body)
        self.write_index([self.row(title="重复登录提示")])

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(result.valid, ())
        self.assertEqual(result.rejected[0].reason, "invalid_body")

    def test_long_qr_image_url_does_not_turn_login_prompt_into_content(self) -> None:
        qr_url = "https://example.com/qr?payload=" + "a" * 2000
        body = f"请登录后继续\n\n![二维码]({qr_url})"
        self.assertGreater(
            sum(not character.isspace() for character in body), 80
        )
        self.write_article(body=body)
        self.write_index([self.row(title="二维码登录提示")])

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(result.valid, ())
        self.assertEqual(result.rejected[0].reason, "invalid_body")

    def test_file_start_frontmatter_is_ignored_for_body_validation(self) -> None:
        frontmatter = (
            "---\n"
            'title: "真实导出文章"\n'
            "published: 2026-07-01T09:30:00+08:00\n"
            "source_url: https://mp.weixin.qq.com/s/frontmatter"
            "?pass_ticket=secret&token=private\n"
            "tags:\n"
            "  - 投资组合\n"
            "  - 微信文章\n"
            "description: 这是一段较长的导出元数据说明，不属于用户可见正文。\n"
            "---   \n"
        )
        bodies = (
            ("仅元数据", frontmatter + " \t\n", "frontmatter-only"),
            ("登录页", frontmatter + "请登录后继续\n", "frontmatter-login"),
            (
                "错误页",
                frontmatter + "此内容因违规无法查看\n",
                "frontmatter-error",
            ),
            ("正常正文", frontmatter + LONG_BODY, "frontmatter-normal"),
        )
        rows: list[dict[str, str]] = []
        for title, body, slug in bodies:
            filename = f"{slug}.md"
            self.write_article(filename, body)
            rows.append(
                self.row(
                    title=title,
                    source_url=f"https://mp.weixin.qq.com/s/{slug}",
                    markdown_path=filename,
                )
            )
        self.write_index(rows)

        result = ingest_account_output(self.project, self.root)

        self.assertEqual([article.title for article in result.valid], ["正常正文"])
        self.assertEqual(
            [(article.title, article.reason) for article in result.rejected],
            [
                ("仅元数据", "invalid_body"),
                ("登录页", "invalid_body"),
                ("错误页", "invalid_body"),
            ],
        )

    def test_visible_text_keeps_content_around_body_divider(self) -> None:
        body = "第一部分是普通正文。\n\n---\n\n第二部分仍然是普通正文。"

        visible = ingest_module._visible_text(body)

        self.assertIn("第一部分是普通正文", visible)
        self.assertIn("第二部分仍然是普通正文", visible)

    def test_rejects_known_download_error_templates(self) -> None:
        markers = (
            "下载失败",
            "获取文章失败",
            "此内容发送失败无法查看",
            "此内容因违规无法查看",
            "该内容已被发布者删除",
            "此内容已被删除",
            "当前内容暂时无法查看",
            "已停止访问该网页",
            "该内容无法查看",
        )
        rows: list[dict[str, str]] = []
        for index, message in enumerate(markers, start=1):
            name = f"error-{index}.md"
            body = message + "这段填充文字只用于保证样本长度足够但不构成有效文章正文。" * 8
            self.assertGreaterEqual(
                sum(not character.isspace() for character in body), 80
            )
            self.write_article(name, body)
            rows.append(
                self.row(
                    title=message,
                    source_url=f"https://mp.weixin.qq.com/s/error-{index}",
                    markdown_path=name,
                )
            )
        self.write_index(rows)

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(
            [article.reason for article in result.rejected],
            ["invalid_body"] * len(markers),
        )

    def test_long_governance_article_may_quote_error_marker_once(self) -> None:
        body = (
            "# 内容治理机制研究\n\n"
            + "本文分析平台治理政策、审核流程、申诉机制和透明度建设。" * 35
            + "研究样本中曾出现提示语“此内容因违规无法查看”，本文仅作引用。"
            + "后续章节继续讨论规则解释、用户权益和治理效果评估。" * 35
        )
        self.assertGreater(
            sum(not character.isspace() for character in body), 1400
        )
        self.write_article(body=body)
        self.write_index([self.row(title="内容治理机制研究")])

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(len(result.valid), 1)
        self.assertEqual(result.rejected, ())

    def test_rejected_urls_never_retain_credentials_query_or_fragment(self) -> None:
        self.write_article()
        self.write_index(
            [
                self.row(
                    title="下载失败",
                    source_url=(
                        "https://user:password@example.com/download"
                        "?pass_ticket=TOPSECRET&token=x#fragment"
                    ),
                    status="failed",
                ),
                self.row(
                    title="无效来源",
                    source_url=(
                        "http://example.com/not-wechat"
                        "?pass_ticket=TOPSECRET&token=x#fragment"
                    ),
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual(
            [article.reason for article in result.rejected],
            ["download_failed", "invalid_url"],
        )
        self.assertEqual(
            [article.source_url for article in result.rejected],
            ["https://example.com/download", "http://example.com/not-wechat"],
        )
        for article in result.rejected:
            self.assertNotIn("?", article.source_url)
            self.assertNotIn("pass_ticket", article.source_url)
            self.assertNotIn("token", article.source_url)
            self.assertNotIn("TOPSECRET", article.source_url)

    def test_duplicate_canonical_key_keeps_first_valid_article(self) -> None:
        self.write_article()
        self.write_index(
            [
                self.row(
                    title="第一篇",
                    source_url="https://mp.weixin.qq.com/s/same?scene=1",
                ),
                self.row(
                    title="重复篇",
                    source_url="https://mp.weixin.qq.com/s/same?pass_ticket=secret",
                ),
            ]
        )

        result = ingest_account_output(self.project, self.root)

        self.assertEqual([article.title for article in result.valid], ["第一篇"])
        self.assertEqual(
            [(article.title, article.reason) for article in result.rejected],
            [("重复篇", "duplicate")],
        )

    def test_reads_utf8_sig_index(self) -> None:
        self.write_article()
        self.write_index([self.row(title="带 BOM 的索引")], encoding="utf-8-sig")
        self.assertTrue((self.root / "index.csv").read_bytes().startswith(codecs.BOM_UTF8))

        result = ingest_account_output(self.project, self.root)

        self.assertEqual([article.title for article in result.valid], ["带 BOM 的索引"])

    def test_all_valid_articles_share_one_batch_collected_at(self) -> None:
        self.write_article()
        self.write_index(
            [
                self.row(title="第一篇"),
                self.row(
                    title="第二篇",
                    source_url="https://mp.weixin.qq.com/s/second-article",
                ),
            ]
        )
        fake_datetime = Mock()
        fake_datetime.fromisoformat.side_effect = datetime.fromisoformat
        fake_datetime.now.side_effect = (
            datetime(2026, 7, 11, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2026, 7, 11, 1, 2, 4, tzinfo=timezone.utc),
        )

        with patch("inno_collector.ingest.datetime", fake_datetime):
            result = ingest_account_output(self.project, self.root)

        self.assertEqual(len(result.valid), 2)
        self.assertEqual(
            {article.collected_at for article in result.valid},
            {"2026-07-11T09:02:03+08:00"},
        )
        fake_datetime.now.assert_called_once_with()


class YamlStringTests(unittest.TestCase):
    def test_returns_json_quoted_string_for_frontmatter_reuse(self) -> None:
        value = '标题: "双引号"\n下一行'

        rendered = yaml_string(value)

        self.assertEqual(rendered, '"标题: \\"双引号\\"\\n下一行"')
        self.assertEqual(json.loads(rendered), value)


if __name__ == "__main__":
    unittest.main()
