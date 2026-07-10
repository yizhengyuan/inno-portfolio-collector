from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_projects
from .diagnostics import sanitize_diagnostic
from .exporter import MooreExporterAdapter
from .package import DeliveryValidationError, build_delivery_zip, lint_vault
from .pipeline import CollectionPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inno-collect")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--projects", type=Path, required=True)
    collect.add_argument("--since", required=True)
    collect.add_argument("--exporter-script", type=Path, required=True)
    collect.add_argument("--exporter-runtime", type=Path, required=True)
    collect.add_argument("--runtime", type=Path, required=True)
    collect.add_argument("--dry-run", action="store_true")
    package = sub.add_parser("package")
    package.add_argument("--vault", type=Path, required=True)
    package_output = package.add_mutually_exclusive_group(required=True)
    package_output.add_argument("--dist", type=Path)
    package_output.add_argument("--output", type=Path)
    lint = sub.add_parser("lint")
    lint.add_argument("--vault", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
