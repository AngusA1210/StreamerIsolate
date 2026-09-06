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
import time
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


# If a backend we launched hasn't started listening within this long, it isn't
# coming up -- stop waiting on it and let a fresh attempt (or an error) happen.
STARTUP_GRACE_SECONDS = 90


def is_our_backend(pid: int) -> bool:
    """True only if that PID is actually our backend.

    PIDs get recycled, so a bare liveness check can latch onto an unrelated
    process and leave us waiting forever on a backend that never existed.
    """
    if not pid_alive(pid):
        return False
    try:
        cmd = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return "streamerisolate" in cmd


def launched_backend_pid() -> int | None:
    """PID of a backend we started that's still alive, if any."""
    try:
        raw = PID_PATH.read_text().strip().split()
        pid = int(raw[0])
        started_at = float(raw[1]) if len(raw) > 1 else 0.0
    except (OSError, ValueError, IndexError):
        return None
    if started_at and time.time() - started_at > STARTUP_GRACE_SECONDS:
        return None
    return pid if is_our_backend(pid) else None


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
        died_recently = False
        try:
            parts = PID_PATH.read_text().strip().split()
            started_at = float(parts[1]) if len(parts) > 1 else 0.0
            died_recently = bool(started_at) and (time.time() - started_at) <= STARTUP_GRACE_SECONDS
        except (OSError, ValueError, IndexError):
            pass
        PID_PATH.unlink(missing_ok=True)
        if died_recently:
            # We launched one moments ago and it exited instead of listening --
            # that's a real failure worth reporting rather than retrying blindly.
            return {
                "ok": False,
                "error": "The backend started but exited. Last output:\n"
                + (log_tail() or "(log empty)"),
                "logPath": str(LOG_PATH),
            }
        # Otherwise the record is just stale (old, or a recycled PID); drop it
        # and start cleanly below.

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
        PID_PATH.write_text(f"{process.pid} {time.time()}")
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


def note_invocation() -> None:
    """Record that we were launched at all, somewhere always writable.

    If the browser can't execute this host -- e.g. the project sits in a
    macOS-protected folder the browser has no access to -- nothing here runs
    and this file simply never appears. That absence is the diagnostic: it
    separates "the browser never launched the host" from "the host ran and
    failed".
    """
    try:
        with open("/tmp/streamerisolate-host.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} host invoked (project: {PROJECT_ROOT})\n")
    except OSError:
        pass


def main() -> None:
    note_invocation()
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
