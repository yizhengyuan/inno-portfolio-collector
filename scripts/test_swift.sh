#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
if [[ -n "${INNO_COLLECTOR_HELPER:-}" ]]; then
  export INNO_COLLECTOR_HELPER="${INNO_COLLECTOR_HELPER:A}"
fi
if [[ -n "${INNO_READER_HELPER:-}" ]]; then
  export INNO_READER_HELPER="${INNO_READER_HELPER:A}"
fi
cd "$ROOT/macos"

FRAMEWORKS="/Library/Developer/CommandLineTools/Library/Developer/Frameworks"
LIBRARIES="/Library/Developer/CommandLineTools/Library/Developer/usr/lib"

if [[ -d "$FRAMEWORKS/Testing.framework" ]]; then
  exec swift test --enable-swift-testing --disable-xctest \
    -Xswiftc -F -Xswiftc "$FRAMEWORKS" \
    -Xlinker "-F$FRAMEWORKS" \
    -Xlinker -rpath -Xlinker "$FRAMEWORKS" \
    -Xlinker -rpath -Xlinker "$LIBRARIES"
fi

exec swift test --enable-swift-testing --disable-xctest
