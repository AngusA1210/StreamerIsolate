#!/bin/bash
# Runs the extension's JS tests with macOS's built-in JavaScriptCore, so there
# is no npm/node dependency just to test a couple of pure functions.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

JSC="/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
if [ ! -x "$JSC" ]; then
  echo "SKIP: JavaScriptCore not found at $JSC" >&2
  exit 0
fi

"$JSC" tests/test_frame_scheduler.js
