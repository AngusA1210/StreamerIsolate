"""Captures a single application's audio directly via a native ScreenCaptureKit
helper (native/capture-app-audio), instead of a virtual audio device. This means
the user's system output device never has to change -- only the target app's
(e.g. a browser's) audio is captured.

macOS only. The helper binary must be built first (see native/capture-app-audio)
and the first run needs the user to approve the "Screen & System Audio Recording"
permission prompt for their terminal -- that can't be granted programmatically.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

BINARY_PATH = Path(__file__).resolve().parent.parent.parent / "native" / "bin" / "capture-app-audio"

# 4 bytes per float32 sample; multiplied by channel count for one frame.
_BYTES_PER_SAMPLE = 4


class AppAudioCapture:
    def __init__(self, app_name: str, samplerate: int, channels: int, binary_path: Path = BINARY_PATH):
        if sys.platform != "darwin":
            raise RuntimeError("App-specific audio capture is currently macOS-only (uses ScreenCaptureKit).")
        if not binary_path.exists():
            raise FileNotFoundError(
                f"Capture helper not found at {binary_path}. Build it first, e.g.:\n"
                f"  swiftc native/capture-app-audio/main.swift -o {binary_path} "
                f"-framework ScreenCaptureKit -framework AVFoundation"
            )

        self.app_name = app_name
        self.samplerate = samplerate
        self.channels = channels
        self.binary_path = binary_path

        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, on_audio) -> None:
        """on_audio: callable(np.ndarray of shape (frames, channels), dtype float32)."""
        self._stop.clear()
        self._process = subprocess.Popen(
            [
                str(self.binary_path),
                "--app", self.app_name,
                "--samplerate", str(self.samplerate),
                "--channels", str(self.channels),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, args=(on_audio,), daemon=True)
        self._reader_thread.start()

        stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_thread.start()

    def _read_loop(self, on_audio) -> None:
        assert self._process is not None and self._process.stdout is not None
        frame_bytes = _BYTES_PER_SAMPLE * self.channels
        # Read a reasonably small number of frames per iteration to keep latency low.
        read_frames = 1024
        read_size = frame_bytes * read_frames

        while not self._stop.is_set():
            raw = self._process.stdout.read(read_size)
            if not raw:
                break
            usable_len = len(raw) - (len(raw) % frame_bytes)
            if usable_len == 0:
                continue
            samples = np.frombuffer(raw[:usable_len], dtype=np.float32).reshape(-1, self.channels)
            on_audio(samples)

    def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            print(f"[capture-app-audio] {line.decode(errors='replace').rstrip()}")

    def stop(self) -> None:
        self._stop.set()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2)
