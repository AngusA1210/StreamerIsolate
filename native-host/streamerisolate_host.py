"""Native messaging host: lets the browser extension start the backend itself,
so day-to-day use never involves a terminal.

Deliberately narrow. It does *not* carry audio -- the extension still talks to
the backend over the local websocket, which is the path that's known to work.
All this does is answer "is the backend up, and if not, start it".

Because the backend needs ~20s to load its models, `ensure-backend` returns as
soon as it has spawned the process rather than blocking. The extension's
existing reconnect loop covers the wait.

Protocol (Chrome/Firefox native messaging): each message is a 4-byte
native-endian length followed by that many bytes of UTF-8 JSON, on stdin and
stdout. Nothing else may ever be written to stdout.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND = PROJECT_ROOT / ".venv" / "bin" / "streamerisolate"
LOG_PATH = PROJECT_ROOT / "backend.log"
# Records the backend we launched. This host process is short-lived -- the
# browser starts it per message and closes it on reply -- so "am I already
# starting one?" has to live on disk, not in memory.
PID_PATH = PROJECT_ROOT / ".backend.pid"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def launched_backend_pid() -> int | None:
    """PID of a backend we started that's still alive, if any."""
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def log_tail(lines: int = 15) -> str:
    try:
        return "".join(LOG_PATH.read_text(errors="replace").splitlines(keepends=True)[-lines:])
    except OSError:
        return ""


def read_message():
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("@I", header)
    payload = sys.stdin.buffer.read(length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def send_message(message) -> None:
    data = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def backend_listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((HOST, PORT)) == 0


def start_backend() -> dict:
    if not BACKEND.exists():
        return {
            "ok": False,
            "error": f"Backend not installed at {BACKEND}. Run scripts/install.sh first.",
        }

    # The extension retries every couple of seconds while the backend loads
    # models, and every one of those retries lands here. Without this guard we
    # spawn a new backend each time -- a dozen processes loading models at once
    # and fighting over the port, which never resolves.
    existing = launched_backend_pid()
    if existing is not None:
        return {"ok": True, "status": "starting", "pid": existing}

    if PID_PATH.exists():
        # We launched one and it died rather than reaching the listening state.
        tail = log_tail()
        PID_PATH.unlink(missing_ok=True)
        return {
            "ok": False,
            "error": "The backend started but exited. Last output:\n" + (tail or "(log empty)"),
            "logPath": str(LOG_PATH),
        }

    try:
        log = open(LOG_PATH, "ab", buffering=0)
        env = dict(os.environ)
        # Without this the backend's own progress output sits in a buffer and
        # never reaches the log, which makes failures undiagnosable.
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [str(BACKEND), "serve"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
            env=env,
            # Detach so the backend outlives this short-lived host process
            # (the browser closes the host as soon as the message is answered).
            start_new_session=True,
        )
        PID_PATH.write_text(str(process.pid))
    except Exception as e:  # noqa: BLE001 - reported to the extension
        return {"ok": False, "error": f"Could not start backend: {type(e).__name__}: {e}"}

    return {
        "ok": True,
        "status": "starting",
        "pid": process.pid,
        "message": "Backend starting; it loads models for ~20s before accepting connections.",
        "logPath": str(LOG_PATH),
    }


def handle(message) -> dict:
    kind = (message or {}).get("type")

    if kind == "ping":
        return {"ok": True, "backendRunning": backend_listening()}

    if kind == "ensure-backend":
        if backend_listening():
            # Someone else's backend (or ours) is up; clear any stale record.
            PID_PATH.unlink(missing_ok=True)
            return {"ok": True, "status": "running"}
        return start_backend()

    return {"ok": False, "error": f"Unknown message type: {kind!r}"}


def main() -> None:
    # Never let a stray print corrupt the framed stdout stream.
    sys.stdout.reconfigure(line_buffering=False)
    while True:
        message = read_message()
        if message is None:
            return
        try:
            send_message(handle(message))
        except Exception as e:  # noqa: BLE001 - keep the host alive
            send_message({"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
