"""Local WebSocket bridge for the Chrome extension.

The extension uses chrome.tabCapture to truly intercept a tab's audio (muting
the tab and taking over what reaches the speakers), streams raw PCM here over
a websocket, gets back isolated-speech PCM, and plays that through its own
AudioContext. This is what makes "replace the tab's audio" actually work,
unlike the ScreenCaptureKit tap explored earlier (see project notes) which
could only add a copy on top of -- not replace -- the original.

Protocol (ws://127.0.0.1:8765 by default):
  client -> server: one JSON text message first:
      {"type": "start", "sampleRate": 48000, "channels": 2,
       "chunkSeconds": 6.0, "overlapSeconds": 1.0, "gain": 1.0}
  server -> client: {"type": "ready"} or {"type": "error", "message": "..."}
  client -> server: binary messages, raw interleaved float32 PCM at the
      declared sampleRate/channels, sent continuously as audio arrives.
  server -> client: binary messages, raw interleaved float32 PCM (isolated
      speech), resampled back to the client's declared sampleRate, in the
      same chunk/hop cadence used by the standalone pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math

import numpy as np
import torch
import websockets
from scipy.signal import resample_poly

from . import audio_io
from .pipeline import Pipeline
from .separator import SpeechIsolator
from .vocal_classifier import ClassifierState, VocalClassifier

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _resample_ratio(src_rate: int, dst_rate: int) -> tuple[int, int]:
    g = math.gcd(src_rate, dst_rate)
    return dst_rate // g, src_rate // g  # (up, down) for scipy.signal.resample_poly


class StreamSession:
    """One capture session's worth of state: buffering, resampling, and the
    same chunked-crossfade Demucs processing used by pipeline.Pipeline, just
    driven by pushed bytes instead of a sounddevice callback.
    """

    def __init__(
        self,
        isolator: SpeechIsolator,
        browser_rate: int,
        channels: int,
        chunk_seconds: float = 3.0,
        overlap_seconds: float = 0.75,
        gain: float = 1.0,
        vocal_classifier: VocalClassifier | None = None,
        vocal_strength: float = 1.0,
    ):
        if overlap_seconds >= chunk_seconds:
            raise ValueError("overlap_seconds must be smaller than chunk_seconds")

        self.isolator = isolator
        self.browser_rate = browser_rate
        self.channels = channels
        self.gain = gain
        self.vocal_classifier = vocal_classifier
        # 0..1, adjustable live from the extension's slider. Kept per-session
        # rather than on the classifier, which is shared across connections.
        self.vocal_strength = vocal_strength
        self.classifier_state = ClassifierState()
        self.model_rate = isolator.samplerate

        self._up, self._down = _resample_ratio(browser_rate, self.model_rate)
        self._up_back, self._down_back = _resample_ratio(self.model_rate, browser_rate)

        self.chunk_samples_browser = int(chunk_seconds * browser_rate)
        self.overlap_samples_browser = int(overlap_seconds * browser_rate)
        self.hop_samples_browser = self.chunk_samples_browser - self.overlap_samples_browser
        self.overlap_samples_model = int(overlap_seconds * self.model_rate)

        self._raw_buffer = np.zeros((0, channels), dtype=np.float32)
        self._prev_tail: np.ndarray | None = None

    def feed(self, raw_bytes: bytes) -> list[bytes]:
        """Push newly captured browser-rate PCM in; get back zero or more
        isolated-speech PCM blocks (at browser_rate) ready to play.
        """
        incoming = np.frombuffer(raw_bytes, dtype=np.float32).reshape(-1, self.channels)
        self._raw_buffer = np.concatenate([self._raw_buffer, incoming], axis=0)

        outputs: list[bytes] = []
        while len(self._raw_buffer) >= self.chunk_samples_browser:
            raw_chunk = self._raw_buffer[: self.chunk_samples_browser].copy()
            self._raw_buffer = self._raw_buffer[self.hop_samples_browser :]

            # Resample the whole multi-second window at once (not per tiny
            # packet) so resampling edge artifacts land only at chunk
            # boundaries, where the crossfade below already smooths seams.
            model_chunk = resample_poly(raw_chunk, self._up, self._down, axis=0).astype(np.float32)
            hop_samples_model = len(model_chunk) - self.overlap_samples_model

            tensor = torch.from_numpy(np.ascontiguousarray(model_chunk.T))
            vocals = self.isolator.isolate_speech(tensor)
            out = vocals.numpy().T * self.gain

            if self.vocal_classifier is not None and self.vocal_strength > 0.0:
                out = self.vocal_classifier.apply(
                    out,
                    self.model_rate,
                    strength=self.vocal_strength,
                    state=self.classifier_state,
                )

            if self._prev_tail is None:
                emit = out[:hop_samples_model]
            else:
                fade_in = np.linspace(0.0, 1.0, self.overlap_samples_model, dtype=np.float32).reshape(-1, 1)
                fade_out = 1.0 - fade_in
                crossfaded = out[: self.overlap_samples_model] * fade_in + self._prev_tail * fade_out
                emit = np.concatenate([crossfaded, out[self.overlap_samples_model : hop_samples_model]], axis=0)
            self._prev_tail = out[hop_samples_model : hop_samples_model + self.overlap_samples_model]

            back = resample_poly(emit, self._up_back, self._down_back, axis=0).astype(np.float32)
            outputs.append(np.ascontiguousarray(back).tobytes())
        return outputs


async def _push_status(websocket, pipeline: Pipeline) -> None:
    """Keeps a control client (the Firefox extension) informed while its
    pipeline runs: whether audio has started flowing yet, how far behind it
    is so the video overlay can match, and any failure.

    The raw delay estimate swings by a whole hop as the output queue fills and
    drains, so it's smoothed here -- feeding that swing straight to the video
    overlay would make the picture visibly jump every few seconds.
    """
    smoothed: float | None = None
    try:
        while True:
            raw = pipeline.estimated_delay_seconds
            smoothed = raw if smoothed is None else 0.25 * raw + 0.75 * smoothed
            await websocket.send(
                json.dumps(
                    {
                        "type": "status",
                        "phase": "running" if pipeline.chunks_emitted > 0 else "buffering",
                        "delaySeconds": round(smoothed, 3),
                        "error": pipeline.error,
                    }
                )
            )
            if pipeline.error:
                return
            await asyncio.sleep(0.5)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
        return


async def _handle_connection(
    websocket, isolator: SpeechIsolator, vocal_classifier: VocalClassifier | None
) -> None:
    session: StreamSession | None = None
    control_pipeline: Pipeline | None = None
    status_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    peer = websocket.remote_address
    print(f"[server] client connected: {peer}")

    def stop_control_pipeline():
        nonlocal control_pipeline, status_task
        if status_task is not None:
            status_task.cancel()
            status_task = None
        if control_pipeline is not None:
            control_pipeline.stop()
            control_pipeline = None

    try:
        async for message in websocket:
            if isinstance(message, str):
                data = json.loads(message)
                if data.get("type") == "start":
                    try:
                        session = StreamSession(
                            isolator=isolator,
                            browser_rate=int(data["sampleRate"]),
                            channels=int(data.get("channels", 2)),
                            chunk_seconds=float(data.get("chunkSeconds", 3.0)),
                            overlap_seconds=float(data.get("overlapSeconds", 0.75)),
                            gain=float(data.get("gain", 1.0)),
                            vocal_classifier=vocal_classifier,
                            vocal_strength=float(data.get("vocalStrength", 1.0)),
                        )
                        await websocket.send(json.dumps({"type": "ready"}))
                        print(f"[server] session started: {data}")
                    except Exception as e:  # noqa: BLE001 - report back to client
                        await websocket.send(json.dumps({"type": "error", "message": str(e)}))
                elif data.get("type") == "settings":
                    # Live adjustment from either extension's strength slider.
                    if "vocalStrength" in data:
                        strength = float(data["vocalStrength"])
                        if session is not None:
                            session.vocal_strength = strength
                        if control_pipeline is not None:
                            control_pipeline.vocal_strength = strength
                        print(f"[server] vocal strength -> {strength:.2f}")
                elif data.get("type") == "stop":
                    session = None

                # --- control mode: the client drives a local device pipeline
                # rather than streaming audio itself. This is how Firefox works,
                # since it can't capture tab audio the way Chrome can.
                elif data.get("type") == "control-hello":
                    devices = audio_io.list_devices()
                    default_in, default_out = audio_io.default_devices()
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "devices",
                                "inputs": [
                                    {"index": d.index, "name": d.name}
                                    for d in devices
                                    if d.max_input_channels > 0
                                ],
                                "outputs": [
                                    {"index": d.index, "name": d.name}
                                    for d in devices
                                    if d.max_output_channels > 0
                                ],
                                "defaultInput": default_in,
                                "defaultOutput": default_out,
                            }
                        )
                    )
                elif data.get("type") == "control-start":
                    try:
                        stop_control_pipeline()
                        control_pipeline = Pipeline(
                            output_device=int(data["outputDevice"]),
                            isolator=isolator,
                            input_device=int(data["inputDevice"]),
                            vocal_classifier=vocal_classifier,
                            vocal_strength=float(data.get("vocalStrength", 1.0)),
                        )
                        control_pipeline.start()
                        status_task = asyncio.create_task(_push_status(websocket, control_pipeline))
                        await websocket.send(json.dumps({"type": "control-started"}))
                        print(f"[server] control pipeline started: {data}")
                    except Exception as e:  # noqa: BLE001 - report back to client
                        stop_control_pipeline()
                        await websocket.send(json.dumps({"type": "error", "message": str(e)}))
                elif data.get("type") == "control-stop":
                    stop_control_pipeline()
                    await websocket.send(json.dumps({"type": "control-stopped"}))
                    print("[server] control pipeline stopped")
            else:
                if session is None:
                    continue
                outputs = await loop.run_in_executor(None, session.feed, message)
                for chunk_bytes in outputs:
                    await websocket.send(chunk_bytes)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # Don't leave a device pipeline running (and holding the audio
        # devices) if the controlling extension goes away.
        stop_control_pipeline()
        print(f"[server] client disconnected: {peer}")


async def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    model_name: str = "htdemucs",
    use_vocal_classifier: bool = True,
) -> None:
    print(f"Loading Demucs model '{model_name}'...")
    isolator = SpeechIsolator(model_name=model_name)
    print(f"Model loaded on device: {isolator.device}")

    vocal_classifier = None
    if use_vocal_classifier:
        print("Loading vocal classifier (PANNs Cnn14, to reduce song-vocal bleed-through)...")
        vocal_classifier = VocalClassifier()
        print("Vocal classifier loaded.")

    async def handler(websocket):
        await _handle_connection(websocket, isolator, vocal_classifier)

    try:
        server = await websockets.serve(handler, host, port, max_size=None)
    except OSError as e:
        # Most often "address already in use": another backend is up, which is
        # fine on its own but confusing if it goes unreported.
        print(f"Could not listen on ws://{host}:{port}: {e}")
        print("Another StreamerIsolate backend is probably already running.")
        raise SystemExit(1)

    async with server:
        print(f"StreamerIsolate bridge server listening on ws://{host}:{port}")
        await asyncio.Future()  # run forever


def main() -> None:
    parser = argparse.ArgumentParser(description="Local WebSocket bridge for the StreamerIsolate browser extension")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default="htdemucs")
    parser.add_argument(
        "--no-vocal-classifier",
        action="store_true",
        help="Disable the PANNs-based singing/speech classifier (skips loading its ~330MB checkpoint)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_server(args.host, args.port, args.model, use_vocal_classifier=not args.no_vocal_classifier))
    except KeyboardInterrupt:
        print("\nStopping...")


if __name__ == "__main__":
    main()
