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

from .separator import SpeechIsolator

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
        chunk_seconds: float = 6.0,
        overlap_seconds: float = 1.0,
        gain: float = 1.0,
    ):
        if overlap_seconds >= chunk_seconds:
            raise ValueError("overlap_seconds must be smaller than chunk_seconds")

        self.isolator = isolator
        self.browser_rate = browser_rate
        self.channels = channels
        self.gain = gain
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


async def _handle_connection(websocket, isolator: SpeechIsolator) -> None:
    session: StreamSession | None = None
    loop = asyncio.get_running_loop()
    peer = websocket.remote_address
    print(f"[server] client connected: {peer}")

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
                            chunk_seconds=float(data.get("chunkSeconds", 6.0)),
                            overlap_seconds=float(data.get("overlapSeconds", 1.0)),
                            gain=float(data.get("gain", 1.0)),
                        )
                        await websocket.send(json.dumps({"type": "ready"}))
                        print(f"[server] session started: {data}")
                    except Exception as e:  # noqa: BLE001 - report back to client
                        await websocket.send(json.dumps({"type": "error", "message": str(e)}))
                elif data.get("type") == "stop":
                    session = None
            else:
                if session is None:
                    continue
                outputs = await loop.run_in_executor(None, session.feed, message)
                for chunk_bytes in outputs:
                    await websocket.send(chunk_bytes)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print(f"[server] client disconnected: {peer}")


async def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, model_name: str = "htdemucs") -> None:
    print(f"Loading Demucs model '{model_name}'...")
    isolator = SpeechIsolator(model_name=model_name)
    print(f"Model loaded on device: {isolator.device}")

    async def handler(websocket):
        await _handle_connection(websocket, isolator)

    async with websockets.serve(handler, host, port, max_size=None):
        print(f"StreamerIsolate bridge server listening on ws://{host}:{port}")
        await asyncio.Future()  # run forever


def main() -> None:
    parser = argparse.ArgumentParser(description="Local WebSocket bridge for the StreamerIsolate browser extension")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default="htdemucs")
    args = parser.parse_args()

    try:
        asyncio.run(run_server(args.host, args.port, args.model))
    except KeyboardInterrupt:
        print("\nStopping...")


if __name__ == "__main__":
    main()
