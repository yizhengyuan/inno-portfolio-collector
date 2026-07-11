#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path


def option(arguments: list[str], name: str) -> str:
    index = arguments.index(name)
    return arguments[index + 1]


def projects() -> list[dict[str, object]]:
    path = os.environ.get("INNO_OFFLINE_PROJECTS")
    if not path:
        raise ValueError("offline project fixture is unavailable")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("invalid offline project fixture")
    return payload


def article(account_id: int, article_id: int | None = None) -> dict[str, object]:
    identifier = article_id or account_id * 100 + 1
    suffix = "incremental" if identifier == 999 else f"account-{account_id}"
    return {
        "id": identifier,
        "url": f"https://mp.weixin.qq.com/s/offline-{suffix}",
        "publish_time": "2026-07-11 09:00:00",
        "title": "离线增量资讯" if identifier == 999 else f"离线项目资讯 {account_id}",
    }


def main() -> int:
    arguments = sys.argv[1:]
    command_index = next(
        (index for index, value in enumerate(arguments) if value.startswith("exporter-")),
        None,
    )
    if command_index is None:
        print(json.dumps({"ok": False, "error": "missing command"}))
        return 2
    command = arguments[command_index]
    command_arguments = arguments[command_index + 1 :]
    rows = projects()

    if command == "exporter-auth-check":
        payload: dict[str, object] = {"ok": True, "status": "valid"}
    elif command == "exporter-accounts":
        payload = {
            "ok": True,
            "accounts": [
                {
                    "id": index,
                    "nickname": row["account"],
                    "alias": row.get("wechat_id", ""),
                }
                for index, row in enumerate(rows, start=1)
            ],
        }
    elif command == "exporter-sync":
        payload = {"ok": True}
    elif command == "exporter-articles":
        account_id = int(option(command_arguments, "--account-id"))
        articles = [article(account_id)]
        if account_id == 1 and os.environ.get("INNO_OFFLINE_REVISION") == "2":
            articles.append(article(account_id, 999))
        payload = {"ok": True, "articles": articles}
    elif command == "exporter-download":
        identifiers = [
            int(value)
            for value in option(command_arguments, "--article-ids").split(",")
            if value
        ]
        output_root = Path(option(command_arguments, "--output-dir"))
        output = output_root / "offline"
        output.mkdir(parents=True, exist_ok=True)
        index_path = output / "index.csv"
        fields = (
            "title", "publish_time", "source_url", "markdown_path",
            "image_dir", "status",
        )
        with index_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for identifier in identifiers:
                account_id = 1 if identifier == 999 else identifier // 100
                row = article(account_id, identifier)
                markdown = f"article-{identifier}.md"
                (output / markdown).write_text(
                    f"# {row['title']}\n\n" + "这是纯本地自动化验收正文。" * 24,
                    encoding="utf-8",
                )
                writer.writerow({
                    "title": row["title"],
                    "publish_time": row["publish_time"],
                    "source_url": row["url"],
                    "markdown_path": markdown,
                    "image_dir": "",
                    "status": "success",
                })
        count = len(identifiers)
        payload = {
            "ok": True,
            "output_dir": str(output),
            "index": str(index_path),
            "selected_count": count,
            "success_count": count,
            "failure_count": 0,
            "skipped_count": 0,
            "failed": [],
            "skipped": [],
        }
    else:
        payload = {"ok": False, "error": "unsupported offline command"}
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
