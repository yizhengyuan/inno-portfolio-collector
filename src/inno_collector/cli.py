from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_projects
from .diagnostics import sanitize_diagnostic
from .exporter import MooreExporterAdapter
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
    sub.add_parser("package")
    sub.add_parser("lint")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "collect":
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
