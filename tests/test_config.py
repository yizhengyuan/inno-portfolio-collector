from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from inno_collector.config import load_projects
from inno_collector.models import (
    IngestResult,
    NormalizedArticle,
    PipelineRunResult,
    ProjectAccount,
    ProjectRunResult,
    RejectedArticle,
    VaultApplyResult,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_PATH = REPO_ROOT / "config" / "projects.json"


class ProjectConfigTests(unittest.TestCase):
    def load_payload(self, payload: object) -> tuple[ProjectAccount, ...]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "projects.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return load_projects(path)

    def test_verified_portfolio_accounts(self) -> None:
        projects = load_projects(PROJECTS_PATH)

        self.assertEqual(len(projects), 10)
        self.assertEqual(len({item.project for item in projects}), 10)
        self.assertEqual(len({item.account for item in projects}), 10)
        self.assertTrue(all(item.confidence == "high" for item in projects))
        mappings = {(item.project, item.account) for item in projects}
        self.assertIn(("雷鸟创新", "RayNeo雷鸟眼镜"), mappings)
        self.assertIn(("天兵科技", "北京天兵科技有限公司"), mappings)
        self.assertIn(("推想", "推想医疗InferVision"), mappings)
        self.assertIn(("乐纯", "乐纯生物LePure"), mappings)
        self.assertIn(("上海傲鲨", "傲鲨智能"), mappings)
        identifiers = {item.project: item.wechat_id for item in projects}
        self.assertEqual(identifiers["上海傲鲨"], "ULS-Robotics")
        self.assertEqual(identifiers["智行者"], "velobotics")

    def test_requires_json_array(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "^projects config must be a JSON array$"
        ):
            self.load_payload({"project": "雷鸟创新"})

    def test_requires_config_items_to_be_json_objects(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "project config item must be a JSON object"
        ):
            self.load_payload(["not an object"])

    def test_enabled_must_be_json_boolean(self) -> None:
        for enabled in ("false", 0, None, ""):
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(
                    ValueError, "^enabled must be a JSON boolean$"
                ):
                    self.load_payload(
                        [
                            {
                                "project": "项目",
                                "account": "账号",
                                "confidence": "high",
                                "enabled": enabled,
                            }
                        ]
                    )

        projects = self.load_payload(
            [{"project": "项目", "account": "账号", "confidence": "high"}]
        )
        self.assertTrue(projects[0].enabled)

    def test_aliases_must_be_json_array(self) -> None:
        for aliases in ("别名", {"alias": True}, None):
            with self.subTest(aliases=aliases):
                with self.assertRaisesRegex(
                    ValueError, "^aliases must be a JSON array$"
                ):
                    self.load_payload(
                        [
                            {
                                "project": "项目",
                                "account": "账号",
                                "confidence": "high",
                                "aliases": aliases,
                            }
                        ]
                    )

    def test_normalizes_null_string_fields_as_empty(self) -> None:
        for field_name in ("project", "account"):
            with self.subTest(field=field_name):
                item = {
                    "project": "项目",
                    "account": "账号",
                    "confidence": "high",
                    field_name: None,
                }
                with self.assertRaisesRegex(
                    ValueError, "^project and account names must not be empty$"
                ):
                    self.load_payload([item])

        projects = self.load_payload(
            [
                {
                    "project": "项目",
                    "account": "账号",
                    "wechat_id": None,
                    "confidence": "high",
                }
            ]
        )
        self.assertEqual(projects[0].wechat_id, "")

        with self.assertRaisesRegex(
            ValueError, "^all enabled account mappings must have high confidence$"
        ):
            self.load_payload(
                [{"project": "项目", "account": "账号", "confidence": None}]
            )

    def test_rejects_blank_project_or_account(self) -> None:
        for field_name in ("project", "account"):
            with self.subTest(field=field_name):
                item = {
                    "project": "项目",
                    "account": "账号",
                    "confidence": "high",
                }
                item[field_name] = "  "
                with self.assertRaisesRegex(
                    ValueError, "^project and account names must not be empty$"
                ):
                    self.load_payload([item])

    def test_rejects_non_high_or_missing_confidence(self) -> None:
        for confidence in ("medium", None):
            with self.subTest(confidence=confidence):
                item = {"project": "项目", "account": "账号"}
                if confidence is not None:
                    item["confidence"] = confidence
                with self.assertRaisesRegex(
                    ValueError,
                    "^all enabled account mappings must have high confidence$",
                ):
                    self.load_payload([item])

    def test_rejects_duplicate_project_or_account_names(self) -> None:
        cases = (
            ("project", "duplicate project name"),
            ("account", "duplicate account name"),
        )
        for field_name, message in cases:
            with self.subTest(field=field_name):
                first = {
                    "project": "项目一",
                    "account": "账号一",
                    "confidence": "high",
                }
                second = {
                    "project": "项目二",
                    "account": "账号二",
                    "confidence": "high",
                }
                second[field_name] = f"  {first[field_name]}  "
                with self.assertRaisesRegex(ValueError, f"^{message}$"):
                    self.load_payload([first, second])

    def test_filters_disabled_items_and_normalizes_fields(self) -> None:
        projects = self.load_payload(
            [
                {
                    "project": "",
                    "account": "",
                    "confidence": "low",
                    "enabled": False,
                },
                {
                    "project": "  雷鸟创新 ",
                    "account": " 雷鸟XR  ",
                    "wechat_id": "  wx-id ",
                    "confidence": " high ",
                    "aliases": ["  雷鸟 ", "", "   ", "TCL RayNeo"],
                },
            ]
        )

        self.assertEqual(
            projects,
            (
                ProjectAccount(
                    project="雷鸟创新",
                    account="雷鸟XR",
                    wechat_id="wx-id",
                    confidence="high",
                    aliases=("雷鸟", "TCL RayNeo"),
                ),
            ),
        )

    def test_normalizes_non_string_json_scalars(self) -> None:
        projects = self.load_payload(
            [
                {
                    "project": 101,
                    "account": 202,
                    "wechat_id": 303,
                    "confidence": "high",
                    "aliases": [404, "  别名  "],
                }
            ]
        )

        self.assertEqual(
            projects,
            (
                ProjectAccount(
                    project="101",
                    account="202",
                    wechat_id="303",
                    confidence="high",
                    aliases=("404", "别名"),
                ),
            ),
        )


class DomainModelTests(unittest.TestCase):
    def test_models_are_frozen_slotted_dataclasses_with_expected_fields(self) -> None:
        contracts = (
            (ProjectAccount, "project account wechat_id confidence enabled aliases"),
            (
                NormalizedArticle,
                "key project account title published source_url collected_at "
                "content_hash body source_markdown source_image_dir",
            ),
            (RejectedArticle, "title source_url reason"),
            (IngestResult, "valid rejected"),
            (
                ProjectRunResult,
                "project account discovered downloaded skipped failed status error "
                "last_sync",
            ),
            (
                PipelineRunResult,
                "projects project_count failed_projects article_count duplicate_count",
            ),
            (VaultApplyResult, "created updated unchanged"),
        )

        for model, field_names in contracts:
            with self.subTest(model=model.__name__):
                names = tuple(field_names.split())
                self.assertTrue(model.__dataclass_params__.frozen)
                self.assertEqual(tuple(field.name for field in fields(model)), names)
                self.assertEqual(model.__slots__, names)

    def test_project_account_defaults(self) -> None:
        self.assertEqual(
            ProjectAccount("项目", "账号"),
            ProjectAccount(
                project="项目",
                account="账号",
                wechat_id="",
                confidence="high",
                enabled=True,
                aliases=(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
