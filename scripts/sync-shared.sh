#!/bin/bash
# Copies the browser-agnostic scripts in shared/ into both extensions.
#
#   ./scripts/sync-shared.sh           # copy
#   ./scripts/sync-shared.sh --check   # exit 1 if any copy is out of date
#
# Copies rather than symlinks because Firefox won't load a symlinked file
# that points outside the add-on's folder (Chrome will). The copies are
# committed so both extension folders load straight from a checkout.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

check=0
[ "${1:-}" = "--check" ] && check=1

stale=0
for src in shared/*.js; do
  for ext in extension-chrome extension-firefox; do
    dest="$ext/$(basename "$src")"
    if cmp -s "$src" "$dest"; then
      continue
    fi
    if [ "$check" = "1" ]; then
      echo "out of date: $dest (run ./scripts/sync-shared.sh)" >&2
      stale=1
    else
      cp "$src" "$dest"
      echo "updated $dest"
    fi
  done
done
exit "$stale"
