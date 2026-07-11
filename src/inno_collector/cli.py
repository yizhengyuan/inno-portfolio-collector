from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_projects
from .dashboard import build_dashboard
from .diagnostics import sanitize_diagnostic
from .draft_package import build_draft_package, receive_draft_package
from .exporter import MooreExporterAdapter
from .package import DeliveryValidationError, build_delivery_zip, lint_vault
from .pipeline import CollectionPipeline
from .update_package import apply_update_package, build_update_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inno-collect")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--projects", type=Path, default=Path("config/projects.json"))
    collect.add_argument("--since", default="2026-01-01")
    collect.add_argument(
        "--exporter-script",
        type=Path,
        default=Path(
            os.environ.get(
                "INNO_EXPORTER_SCRIPT",
                "../moore-wechat-article-downloader/scripts/wechat_exporter.py",
            )
        ).expanduser(),
    )
    collect.add_argument(
        "--exporter-runtime",
        type=Path,
        default=Path(
            os.environ.get(
                "INNO_EXPORTER_RUNTIME",
                "~/.moore/wechat-article-downloader",
            )
        ).expanduser(),
    )
    collect.add_argument("--runtime", type=Path, default=Path("runtime"))
    collect.add_argument("--dry-run", action="store_true")
    package = sub.add_parser("package")
    package.add_argument(
        "--vault", type=Path, default=Path("runtime/vault/英诺被投项目资讯库")
    )
    package_output = package.add_mutually_exclusive_group()
    package_output.add_argument("--dist", type=Path, default=Path("dist"))
    package_output.add_argument("--output", type=Path)
    lint = sub.add_parser("lint")
    lint.add_argument(
        "--vault", type=Path, default=Path("runtime/vault/英诺被投项目资讯库")
    )
    package_update = sub.add_parser("package-update")
    package_update.add_argument("--vault", type=Path, required=True)
    package_update.add_argument("--output", type=Path, required=True)
    package_update.add_argument("--base-package", type=Path)
    package_update.add_argument("--created-at")
    apply_update = sub.add_parser("apply-update")
    apply_update.add_argument("--package", type=Path, required=True)
    apply_update.add_argument("--vault", type=Path, required=True)
    package_drafts = sub.add_parser("package-drafts")
    package_drafts.add_argument("--vault", type=Path, required=True)
    package_drafts.add_argument("--draft", action="append", required=True)
    package_drafts.add_argument("--output", type=Path, required=True)
    package_drafts.add_argument("--exported-at", required=True)
    receive_drafts = sub.add_parser("receive-drafts")
    receive_drafts.add_argument("--package", type=Path, required=True)
    receive_drafts.add_argument("--inbox", type=Path, required=True)
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--vault", type=Path, required=True)
    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {
        "package-update",
        "apply-update",
        "package-drafts",
        "receive-drafts",
        "dashboard",
    }:
        try:
            if args.command == "package-update":
                result = build_update_package(
                    args.vault,
                    args.output,
                    base_package=args.base_package,
                    created_at=args.created_at,
                )
            elif args.command == "apply-update":
                result = asdict(apply_update_package(args.package, args.vault))
            elif args.command == "package-drafts":
                result = build_draft_package(
                    args.vault,
                    args.draft,
                    args.output,
                    exported_at=args.exported_at,
                )
            elif args.command == "receive-drafts":
                result = receive_draft_package(args.package, args.inbox)
            else:
                result = {"dashboard_path": str(build_dashboard(args.vault))}
        except Exception as exc:
            _print_json({"error": sanitize_diagnostic(exc)})
            return 2
        _print_json(result)
        return 0

    if args.command == "lint":
        try:
            result = lint_vault(args.vault)
        except Exception as exc:
            print(
                json.dumps(
                    {"error": sanitize_diagnostic(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if result["errors"] else 0

    if args.command == "package":
        try:
            result = build_delivery_zip(args.vault, args.output or args.dist)
        except DeliveryValidationError as exc:
            print(
                json.dumps(exc.report, ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
            )
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {"error": sanitize_diagnostic(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        serializable = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in result.items()
        }
        print(json.dumps(serializable, ensure_ascii=False, sort_keys=True))
        return 0

    try:
        projects = load_projects(args.projects)
        backend = MooreExporterAdapter(args.exporter_script, args.exporter_runtime)
        pipeline = CollectionPipeline(backend, runtime_dir=args.runtime)
        result = pipeline.run(
            projects,
            since=args.since,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(
            f"collection failed: {sanitize_diagnostic(exc)}",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 1 if result.failed_projects else 0
