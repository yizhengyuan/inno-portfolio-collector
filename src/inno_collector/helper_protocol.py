from __future__ import annotations

import json
from collections.abc import Callable
from typing import TextIO

from .diagnostics import sanitize_diagnostic


Handler = Callable[[dict[str, object]], dict[str, object]]


def run_helper(
    handlers: dict[str, Handler],
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    request_id = ""
    try:
        request = json.loads(input_stream.read())
        if not isinstance(request, dict) or set(request) != {"id", "command", "arguments"}:
            raise ValueError("invalid helper request")
        request_id = request["id"]
        command = request["command"]
        arguments = request["arguments"]
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(command, str)
            or not command
            or not isinstance(arguments, dict)
        ):
            raise ValueError("invalid helper request")
        handler = handlers.get(command)
        if handler is None:
            raise ValueError("unsupported helper command")
        result = handler(arguments)
        if not isinstance(result, dict):
            raise ValueError("invalid helper result")
        response = {"id": request_id, "ok": True, "result": result}
        exit_code = 0
    except Exception as exc:
        response = {
            "id": request_id,
            "ok": False,
            "error": sanitize_diagnostic(exc),
        }
        exit_code = 2
    output_stream.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code
