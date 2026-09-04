from __future__ import annotations

import argparse
import time

from . import audio_io
from .pipeline import Pipeline
from .separator import SpeechIsolator
from .server import DEFAULT_HOST, DEFAULT_PORT, run_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove background music from a livestream's audio in near-real-time, "
        "keeping only detected speech."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-devices", help="List available audio input/output devices")

    run = sub.add_parser("run", help="Start the capture -> isolate -> playback pipeline")
    input_group = run.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input", help="Input device index or name substring (virtual audio device)")
    input_group.add_argument(
        "--capture-app",
        help="Capture a single running app's audio directly (e.g. \"Chrome\"), no virtual device or "
        "system output change needed. macOS only.",
    )
    run.add_argument("--output", required=True, help="Output device index or name substring")
    run.add_argument("--model", default="htdemucs", help="Demucs model name (default: htdemucs)")
    run.add_argument("--chunk-seconds", type=float, default=6.0)
    run.add_argument("--overlap-seconds", type=float, default=1.0)
    run.add_argument("--gain", type=float, default=1.0, help="Output gain applied to isolated speech")

    serve = sub.add_parser(
        "serve", help="Run the local WebSocket bridge for the Chrome extension (true tab-audio interception)"
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--model", default="htdemucs", help="Demucs model name (default: htdemucs)")

    args = parser.parse_args()

    if args.command == "list-devices":
        audio_io.print_devices()
        return

    if args.command == "serve":
        import asyncio

        try:
            asyncio.run(run_server(args.host, args.port, args.model))
        except KeyboardInterrupt:
            print("\nStopping...")
        return

    if args.command == "run":
        output_device = audio_io.resolve_device(args.output)

        print(f"Loading Demucs model '{args.model}'...")
        isolator = SpeechIsolator(model_name=args.model)
        print(f"Model loaded on device: {isolator.device}")

        if args.capture_app:
            pipeline = Pipeline(
                output_device=output_device,
                isolator=isolator,
                capture_app=args.capture_app,
                chunk_seconds=args.chunk_seconds,
                overlap_seconds=args.overlap_seconds,
                gain=args.gain,
            )
        else:
            input_device = audio_io.resolve_device(args.input)
            pipeline = Pipeline(
                output_device=output_device,
                isolator=isolator,
                input_device=input_device,
                chunk_seconds=args.chunk_seconds,
                overlap_seconds=args.overlap_seconds,
                gain=args.gain,
            )

        print(
            f"Starting pipeline (chunk={args.chunk_seconds}s, overlap={args.overlap_seconds}s). "
            "Expect roughly a chunk-length delay before speech-only audio starts playing. "
            "Press Ctrl+C to stop."
        )
        pipeline.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            pipeline.stop()


if __name__ == "__main__":
    main()
