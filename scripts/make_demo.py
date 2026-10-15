"""Regenerates the README's before/after figure.

Mixes speech (macOS `say`, so there's nothing copyrighted to ship) over
synthesized music, runs it through the same StreamSession path the
extensions use -- 48kHz in, resampled, chunked, Demucs, classifier,
crossfaded, back out -- and plots spectrograms of the reference speech, the
mix, and the result. Also writes the audio to build/demo/ for listening.

    python scripts/make_demo.py

Needs the full install (models) and matplotlib (`pip install -e '.[demo]'`).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, spectrogram

RATE = 48000
LEAD_IN = 3.0
TAIL = 3.0
SPEECH = (
    "Okay chat, welcome back. So the plan for tonight is to finish the last "
    "two bosses, and then we'll do some viewer games if there's time left. "
    "Let me know in chat which one you want first."
)

ROOT = Path(__file__).resolve().parent.parent
FIGURE = ROOT / "docs" / "images" / "demo-spectrogram.png"
AUDIO_DIR = ROOT / "build" / "demo"


def tts(text: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "speech.wav"
        subprocess.run(
            ["say", "-o", str(path), f"--data-format=LEF32@{RATE}", text], check=True
        )
        audio, rate = sf.read(path, dtype="float32")
    assert rate == RATE
    return audio if audio.ndim == 1 else audio.mean(axis=1)


def synth_music(seconds: float, seed: int = 0) -> np.ndarray:
    """Pads, bass, kick and hats at 120bpm over Am-F-C-G. Stereo."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    bar = 2.0
    chords = [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)]

    def hz(midi):
        return 440.0 * 2 ** ((midi - 69) / 12)

    def saw(freq):
        return 2.0 * ((t * freq) % 1.0) - 1.0

    pad_l = np.zeros(n)
    pad_r = np.zeros(n)
    bass = np.zeros(n)
    for i in range(int(np.ceil(seconds / bar))):
        start, end = int(i * bar * RATE), min(int((i + 1) * bar * RATE), n)
        notes = chords[i % len(chords)]
        for note in notes:
            pad_l[start:end] += saw(hz(note) * 0.997)[start:end]
            pad_r[start:end] += saw(hz(note) * 1.003)[start:end]
        bass[start:end] = np.sin(2 * np.pi * hz(notes[0] - 24) * t[start:end])

    lowpass = butter(2, 1800, fs=RATE, output="sos")
    pad_l, pad_r = sosfilt(lowpass, pad_l) / 3, sosfilt(lowpass, pad_r) / 3

    drums = np.zeros(n)
    beat = int(0.5 * RATE)
    kick_len = int(0.25 * RATE)
    kt = np.arange(kick_len) / RATE
    kick = np.sin(2 * np.pi * (50 + 100 * np.exp(-kt * 30)) * kt) * np.exp(-kt * 12)
    hat_len = int(0.04 * RATE)
    highpass = butter(4, 7000, btype="high", fs=RATE, output="sos")
    hat = sosfilt(highpass, rng.standard_normal(hat_len)) * np.exp(-np.arange(hat_len) / (0.008 * RATE))
    for pos in range(0, n, beat):
        drums[pos : pos + kick_len] += kick[: n - pos]
        off = pos + beat // 2
        if off < n:
            drums[off : off + hat_len] += 0.3 * hat[: n - off]

    left = 0.45 * pad_l + 0.5 * bass + 0.6 * drums
    right = 0.45 * pad_r + 0.5 * bass + 0.6 * drums
    return np.stack([left, right], axis=1).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)) + 1e-12))


def plot(panels: list[tuple[str, np.ndarray]], path: Path, dark: bool) -> None:
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    # Matches GitHub's dark theme background so the figure sits flush on it.
    style = {"figure.facecolor": "#0d1117", "axes.facecolor": "#0d1117"} if dark else {}
    with plt.style.context(["dark_background"] if dark else ["default"]), plt.rc_context(style):
        fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True, constrained_layout=True)
        for ax, (title, signal) in zip(axes, panels):
            f, t, sxx = spectrogram(signal, fs=RATE, nperseg=2048, noverlap=1536)
            keep = f <= 8000
            ax.pcolormesh(
                t, f[keep] / 1000, 10 * np.log10(sxx[keep] + 1e-10),
                vmin=-110, vmax=-40, cmap="magma", shading="auto", rasterized=True,
            )
            ax.set_title(title, loc="left", fontsize=11)
            ax.set_ylabel("kHz")
        axes[-1].set_xlabel("seconds")
        fig.suptitle(
            "Synthetic test: macOS `say` speech over synthesized music, "
            "through the same code path the extensions use",
            fontsize=11,
        )
        buf = io.BytesIO()
        fig.savefig(buf, dpi=110, format="png")
        plt.close(fig)
    # Spectrogram noise defeats PNG compression; a 256-colour palette is
    # visually identical here and a fraction of the size.
    Image.open(buf).convert("RGB").quantize(256).save(path, optimize=True)


def main() -> None:
    from streamerisolate.separator import SpeechIsolator
    from streamerisolate.server import StreamSession
    from streamerisolate.vocal_classifier import VocalClassifier

    speech = tts(SPEECH)
    total = int((LEAD_IN + len(speech) / RATE + TAIL) * RATE)
    speech_track = np.zeros(total, dtype=np.float32)
    s0 = int(LEAD_IN * RATE)
    speech_track[s0 : s0 + len(speech)] = speech
    speech_track *= 0.25 / rms(speech)  # speech-active level

    music = synth_music(total / RATE)
    music *= 0.18 / rms(music)  # loud enough to compete with the voice
    mix = music + speech_track[:, None]
    mix /= max(1.0, float(np.abs(mix).max()) / 0.95)

    print("Loading models...", file=sys.stderr)
    session = StreamSession(
        isolator=SpeechIsolator(),
        browser_rate=RATE,
        channels=2,
        vocal_classifier=VocalClassifier(),
    )
    blocks = session.feed(mix.astype(np.float32).tobytes())
    out = np.frombuffer(b"".join(blocks), dtype=np.float32).reshape(-1, 2)

    n = min(len(out), len(mix))
    mix_m, out_m, ref_m = mix[:n].mean(axis=1), out[:n].mean(axis=1), speech_track[:n]

    music_only = np.zeros(n, dtype=bool)
    music_only[int(0.5 * RATE) : s0 - int(0.25 * RATE)] = True
    music_only[s0 + len(speech) + int(0.25 * RATE) : n] = True
    reduction_db = 20 * np.log10(rms(mix_m[music_only]) / rms(out_m[music_only]))
    in_speech = slice(s0, min(s0 + len(speech), n))
    correlation = float(np.corrcoef(out_m[in_speech], ref_m[in_speech])[0, 1])
    print(f"music-only stretches: {reduction_db:.1f} dB quieter", file=sys.stderr)
    print(f"output vs. clean speech correlation: {correlation:.3f}", file=sys.stderr)

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(AUDIO_DIR / "input.wav", mix[:n], RATE)
    sf.write(AUDIO_DIR / "output.wav", out[:n], RATE)
    sf.write(AUDIO_DIR / "speech-reference.wav", speech_track[:n], RATE)

    panels = [
        ("Input: speech over music", mix_m),
        ("Output: what you hear", out_m),
        ("Clean speech (reference, never shown to the model)", ref_m),
    ]
    FIGURE.parent.mkdir(parents=True, exist_ok=True)
    plot(panels, FIGURE, dark=False)
    plot(panels, FIGURE.with_name(FIGURE.stem + "-dark.png"), dark=True)
    print(f"wrote {FIGURE.parent.relative_to(ROOT)}/ and {AUDIO_DIR.relative_to(ROOT)}/", file=sys.stderr)


if __name__ == "__main__":
    main()
