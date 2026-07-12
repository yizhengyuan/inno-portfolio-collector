from __future__ import annotations

import sys


_SMOKE_OUTPUT = '{"role":"collector-web","protocol":1}\n'


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--smoke"]:
        sys.stdout.write(_SMOKE_OUTPUT)
        sys.stdout.flush()
        return 0

    # Keep this import after the smoke branch: build and launch checks must not
    # create a support directory, bind a socket, or load the Moore runtime.
    from inno_collector.web.server import main as server_main

    return server_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
