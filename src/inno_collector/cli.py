from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inno-collect")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect")
    sub.add_parser("package")
    sub.add_parser("lint")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
