#!/bin/bash
# One-time setup for StreamerIsolate.
#
# Installs the backend, fetches the models, and registers the native messaging
# host so the browser extension can start everything itself. After this, using
# StreamerIsolate never involves a terminal.
#
#   ./scripts/install.sh                       # Firefox (+ Chrome if ID given)
#   ./scripts/install.sh <chrome-extension-id>
#   ./scripts/install.sh --dev [<chrome-id>]   # run from the checkout instead
#
# The Chrome extension ID is shown on chrome://extensions with Developer mode
# on. Firefox needs no ID -- the extension declares a fixed one.
#
# By default the runtime is installed to ~/Library/Application Support, NOT run
# from this checkout. That's deliberate: macOS restricts app access to
# Documents, Desktop and Downloads, and browsers launch the native host
# directly (child processes inherit the browser's grants). A checkout in any of
# those folders -- Downloads being the obvious one for a downloaded repo --
# leaves the browser unable to start the backend at all. Application Support is
# unprotected and is where an installed app belongs anyway.
#
# --dev installs editable from the checkout instead, so code changes take
# effect without reinstalling. Only use it if the checkout is somewhere
# unprotected.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_NAME="com.angusa1210.streamerisolate"
FIREFOX_EXT_ID="streamerisolate@angusa1210.github.io"
SUPPORT_DIR="$HOME/Library/Application Support/StreamerIsolate"

DEV_MODE=0
if [ "${1:-}" = "--dev" ]; then
  DEV_MODE=1
  shift
fi
CHROME_EXT_ID="${1:-}"

if [ "$DEV_MODE" = "1" ]; then
  VENV="$PROJECT_ROOT/.venv"
  HOST_DIR="$PROJECT_ROOT/native-host"
else
  VENV="$SUPPORT_DIR/venv"
  HOST_DIR="$SUPPORT_DIR"
fi

echo "==> StreamerIsolate setup"
echo "    source:  $PROJECT_ROOT"
echo "    runtime: $VENV"

if [ "$DEV_MODE" = "1" ]; then
  case "$PROJECT_ROOT" in
    "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
      protected_dir="$(echo "${PROJECT_ROOT#"$HOME"/}" | cut -d/ -f1)"
      echo
      echo "    !! --dev with the checkout in ~/$protected_dir, which macOS protects."
      echo "       Browsers cannot launch the backend from there. Either move the"
      echo "       checkout somewhere unprotected (e.g. ~/Developer), or drop --dev"
      echo "       so the runtime is installed to Application Support instead."
      echo
      ;;
  esac
fi

# --- 1. Python environment -------------------------------------------------
PYTHON=""
for candidate in python3.12 python3.11 python3.10 /opt/homebrew/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
done
if [ -z "$PYTHON" ]; then
  echo "ERROR: needs Python 3.10 or newer (macOS's built-in 3.9 is too old for PyTorch)." >&2
  echo "       Install it with:  brew install python@3.12" >&2
  exit 1
fi

mkdir -p "$HOST_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Creating virtual environment ($($PYTHON --version))"
  "$PYTHON" -m venv "$VENV"
else
  echo "==> Reusing existing virtual environment"
fi

echo "==> Installing dependencies (this can take several minutes the first time)"
"$VENV/bin/pip" install --quiet --upgrade pip

# PyTorch is a large download and a dropped connection mid-install otherwise
# ends the script with a stack trace and a half-built environment. Retry a few
# times; already-downloaded packages are cached, so retries pick up where the
# last attempt got to.
pip_install_with_retries() {
  local attempt
  for attempt in 1 2 3; do
    if "$VENV/bin/pip" install --quiet --timeout 60 --retries 5 "$@"; then
      return 0
    fi
    echo "    download interrupted; retrying ($attempt/3)…"
    sleep 3
  done
  echo "ERROR: could not install dependencies. Re-run this script to resume." >&2
  return 1
}

if [ "$DEV_MODE" = "1" ]; then
  # Editable, so edits to the checkout take effect immediately.
  pip_install_with_retries -e "$PROJECT_ROOT"
else
  # A real copy, so the runtime doesn't depend on the checkout still being
  # there (or being readable by the browser).
  pip_install_with_retries "$PROJECT_ROOT"
  cp "$PROJECT_ROOT/native-host/streamerisolate_host.py" "$HOST_DIR/"
fi

# --- 2. Models -------------------------------------------------------------
# The classifier's package downloads its checkpoint with wget, which macOS
# doesn't ship, so fetch it here with curl instead. Demucs fetches its own
# weights fine on first run.
PANNS_DIR="$HOME/panns_data"
PANNS_CKPT="$PANNS_DIR/Cnn14_mAP=0.431.pth"
PANNS_LABELS="$PANNS_DIR/class_labels_indices.csv"
mkdir -p "$PANNS_DIR"

if [ ! -f "$PANNS_LABELS" ]; then
  echo "==> Downloading classifier labels"
  curl -fsSL -o "$PANNS_LABELS" \
    "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv"
fi

# 3e8 is the size the classifier package itself treats as "complete".
if [ ! -f "$PANNS_CKPT" ] || [ "$(stat -f%z "$PANNS_CKPT")" -lt 300000000 ]; then
  echo "==> Downloading classifier model (~327MB, resumable)"
  for _ in $(seq 1 20); do
    size=$(stat -f%z "$PANNS_CKPT" 2>/dev/null || echo 0)
    [ "$size" -ge 327428481 ] && break
    curl -fsSL -C - -o "$PANNS_CKPT" --max-time 120 \
      "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1" || true
  done
  if [ "$(stat -f%z "$PANNS_CKPT" 2>/dev/null || echo 0)" -lt 300000000 ]; then
    echo "ERROR: classifier model download incomplete. Re-run this script to resume." >&2
    exit 1
  fi
fi

# --- 3. Native messaging host ---------------------------------------------
LAUNCHER="$HOST_DIR/run-host.sh"
cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/bin/sh
# Generated by scripts/install.sh -- absolute paths are required here because
# browsers launch this directly, with no shell environment to speak of.
exec "$VENV/bin/python" "$HOST_DIR/streamerisolate_host.py"
LAUNCHER_EOF
chmod +x "$LAUNCHER"

write_manifest() {
  local target_dir="$1" browser="$2" allow_key="$3" allow_value="$4"
  mkdir -p "$target_dir"
  cat > "$target_dir/$HOST_NAME.json" <<MANIFEST_EOF
{
  "name": "$HOST_NAME",
  "description": "Starts the StreamerIsolate backend on demand",
  "path": "$LAUNCHER",
  "type": "stdio",
  "$allow_key": [$allow_value]
}
MANIFEST_EOF
  echo "    registered for $browser"
}

echo "==> Registering native messaging host"
write_manifest "$HOME/Library/Application Support/Mozilla/NativeMessagingHosts" \
  "Firefox" "allowed_extensions" "\"$FIREFOX_EXT_ID\""

if [ -n "$CHROME_EXT_ID" ]; then
  # Both Chrome and Chromium may be installed; label them distinctly so the
  # output doesn't just say "Chrome" twice.
  chrome_base="$HOME/Library/Application Support/Google/Chrome"
  chromium_base="$HOME/Library/Application Support/Chromium"
  [ -d "$chrome_base" ] && write_manifest "$chrome_base/NativeMessagingHosts" "Chrome" \
    "allowed_origins" "\"chrome-extension://$CHROME_EXT_ID/\""
  [ -d "$chromium_base" ] && write_manifest "$chromium_base/NativeMessagingHosts" "Chromium" \
    "allowed_origins" "\"chrome-extension://$CHROME_EXT_ID/\""
else
  echo "    skipped Chrome (no extension ID given)"
fi

echo
echo "==> Done."
echo
echo "Next steps:"
echo "  Firefox:  about:debugging#/runtime/this-firefox -> Load Temporary Add-on"
echo "            -> pick extension-firefox/manifest.json"
echo "  Chrome:   chrome://extensions -> Developer mode -> Load unpacked"
echo "            -> pick extension-chrome/"
if [ -z "$CHROME_EXT_ID" ]; then
  echo
  echo "  For Chrome, re-run with the extension ID shown on chrome://extensions:"
  echo "      ./scripts/install.sh <chrome-extension-id>"
fi
echo
echo "Then just click the extension icon -- it starts the backend for you."
