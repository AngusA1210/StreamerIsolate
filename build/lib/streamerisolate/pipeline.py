"""Streaming pipeline: capture -> chunk -> Demucs separate -> crossfade -> output.

This is a near-real-time pipeline: it introduces a delay of roughly one
chunk length (a few seconds) because Demucs needs a contiguous window of
audio to separate well. That tradeoff was a deliberate v1 scoping decision
-- true zero-latency separation is a much harder problem.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
import sounddevice as sd
import torch

from .app_capture import AppAudioCapture
from .separator import SpeechIsolator
from .vocal_classifier import ClassifierState, VocalClassifier


class Pipeline:
    def __init__(
        self,
        output_device: int,
        isolator: SpeechIsolator,
        input_device: int | None = None,
        capture_app: str | None = None,
        chunk_seconds: float = 3.0,
        overlap_seconds: float = 0.75,
        gain: float = 1.0,
        block_size: int = 1024,
        vocal_classifier: VocalClassifier | None = None,
        vocal_strength: float = 1.0,
    ):
        if overlap_seconds >= chunk_seconds:
            raise ValueError("overlap_seconds must be smaller than chunk_seconds")
        if (input_device is None) == (capture_app is None):
            raise ValueError("exactly one of input_device or capture_app must be given")

        self.input_device = input_device
        self.capture_app = capture_app
        self.output_device = output_device
        self.isolator = isolator
        self.vocal_classifier = vocal_classifier
        self.vocal_strength = vocal_strength
        self.classifier_state = ClassifierState()
        self.samplerate = isolator.samplerate
        self.channels = isolator.audio_channels
        self.gain = gain
        self.block_size = block_size

        self.chunk_seconds = chunk_seconds
        self.chunk_samples = int(chunk_seconds * self.samplerate)
        self.overlap_samples = int(overlap_seconds * self.samplerate)
        self.hop_samples = self.chunk_samples - self.overlap_samples

        self._input_lock = threading.Lock()
        self._input_buffer = np.zeros((0, self.channels), dtype=np.float32)
        self._output_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._playback_tail = np.zeros((0, self.channels), dtype=np.float32)
        self._prev_tail: np.ndarray | None = None

        # Progress/health, for callers that want to show status (the GUI polls
        # these): chunks_emitted stays 0 until the first chunk is buffered and
        # processed, i.e. while the user is still waiting for audio.
        self.chunks_emitted = 0
        self.error: str | None = None
        self._last_process_seconds = 0.0

        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._instream: sd.InputStream | None = None
        self._outstream: sd.OutputStream | None = None
        self._app_capture: AppAudioCapture | None = None

    # --- capture ---

    def _feed_input(self, samples: np.ndarray) -> None:
        with self._input_lock:
            self._input_buffer = np.concatenate([self._input_buffer, samples], axis=0)

    def _input_callback(self, indata, frames, time_info, status):
        if status:
            print(f"[input] {status}")
        self._feed_input(indata.copy())

    # --- playback ---

    def _output_callback(self, outdata, frames, time_info, status):
        if status:
            print(f"[output] {status}")
        chunks = [self._playback_tail]
        have = len(self._playback_tail)
        self._playback_tail = np.zeros((0, self.channels), dtype=np.float32)
        while have < frames:
            try:
                block = self._output_queue.get_nowait()
            except queue.Empty:
                chunks.append(np.zeros((frames - have, self.channels), dtype=np.float32))
                have = frames
                break
            chunks.append(block)
            have += len(block)
        buf = np.concatenate(chunks, axis=0)
        outdata[:] = buf[:frames]
        self._playback_tail = buf[frames:]

    # --- processing loop ---

    def _process_loop(self):
        while not self._stop.is_set():
            try:
                self._process_once()
            except Exception as e:  # noqa: BLE001
                # Without this the worker thread would die silently and the
                # pipeline would just go quiet with no explanation. Record it
                # so a caller (e.g. the GUI) can surface it.
                self.error = f"{type(e).__name__}: {e}"
                print(f"[pipeline] processing stopped: {self.error}")
                return

    def _process_once(self):
        with self._input_lock:
            available = len(self._input_buffer)
        if available < self.chunk_samples:
            time.sleep(0.05)
            return

        with self._input_lock:
            chunk = self._input_buffer[: self.chunk_samples].copy()
            self._input_buffer = self._input_buffer[self.hop_samples :]

        started = time.time()
        tensor = torch.from_numpy(chunk.T.astype(np.float32))  # (channels, samples)
        vocals = self.isolator.isolate_speech(tensor)  # (channels, samples)
        out = vocals.numpy().T * self.gain  # (samples, channels)

        if self.vocal_classifier is not None and self.vocal_strength > 0.0:
            out = self.vocal_classifier.apply(
                out,
                self.samplerate,
                strength=self.vocal_strength,
                state=self.classifier_state,
            )

        if self._prev_tail is None:
            emit = out[: self.hop_samples]
        else:
            fade_in = np.linspace(0.0, 1.0, self.overlap_samples, dtype=np.float32).reshape(-1, 1)
            fade_out = 1.0 - fade_in
            crossfaded = out[: self.overlap_samples] * fade_in + self._prev_tail * fade_out
            emit = np.concatenate([crossfaded, out[self.overlap_samples : self.hop_samples]], axis=0)

        self._prev_tail = out[self.hop_samples : self.hop_samples + self.overlap_samples]
        self._output_queue.put(emit)
        self.chunks_emitted += 1
        self._last_process_seconds = time.time() - started

    @property
    def estimated_delay_seconds(self) -> float:
        """Roughly how far the played audio lags the live input.

        A full chunk has to accumulate before processing can start, then that
        chunk takes some time to process, and anything already queued plays
        first. Used by the Firefox extension to delay its video overlay to
        match (Chrome measures this directly instead -- it can see both ends).
        """
        queued_seconds = self._output_queue.qsize() * self.hop_samples / self.samplerate
        return self.chunk_seconds + self._last_process_seconds + queued_seconds

    # --- lifecycle ---

    def start(self):
        self._stop.clear()
        self._worker = threading.Thread(target=self._process_loop, daemon=True)
        self._worker.start()

        if self.capture_app is not None:
            self._app_capture = AppAudioCapture(
                app_name=self.capture_app, samplerate=self.samplerate, channels=self.channels
            )
            self._app_capture.start(self._feed_input)
        else:
            self._instream = sd.InputStream(
                device=self.input_device,
                channels=self.channels,
                samplerate=self.samplerate,
                blocksize=self.block_size,
                dtype="float32",
                callback=self._input_callback,
            )
            self._instream.start()

        self._outstream = sd.OutputStream(
            device=self.output_device,
            channels=self.channels,
            samplerate=self.samplerate,
            blocksize=self.block_size,
            dtype="float32",
            callback=self._output_callback,
        )
        self._outstream.start()

    def stop(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)
        if self._app_capture is not None:
            self._app_capture.stop()
        for stream in (self._instream, self._outstream):
            if stream is not None:
                stream.stop()
                stream.close()
