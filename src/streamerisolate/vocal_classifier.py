"""Reduces song-vocal bleed-through in the Demucs 'vocals' stem.

Demucs's vocals stem separates singing-like content from instrumentals, but
it has no notion of "the streamer talking" vs. "a song's singing" -- both
are vocal-like and land in the same stem. This module runs a pretrained
general-purpose audio tagger (PANNs Cnn14, trained on AudioSet) over short
windows of the isolated vocals and attenuates windows that look more like
singing than speech, producing a smooth per-sample gain envelope rather
than a hard on/off switch (to avoid clicky artifacts at boundaries).

Scoring notes, learned the hard way: AudioSet's "Speech" label also fires on
singing, so an earlier rule of "attenuate when singing beats speech by a
fixed margin" almost never triggered strongly -- singing bled through even
at maximum strength. Instead we use the *relative dominance* of singing over
speech, and let `strength` scale both the trigger threshold and how fast
attenuation ramps in, so a high strength setting genuinely cuts hard rather
than just lowering a floor that was rarely reached.

It's still a mitigation, not a fix: when the streamer talks *over* a song,
both are present in the same window and no per-window gate can separate
them.
"""

from __future__ import annotations

import math

import numpy as np
import torch
from panns_inference import AudioTagging
from scipy.signal import resample_poly

PANNS_SAMPLE_RATE = 32000

# Below this, neither label is saying anything meaningful (near-silence or
# non-vocal residue), so we hold gain open rather than acting on noise.
MIN_EVIDENCE = 0.05

SPEECH_LABELS = [
    "Speech",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Child speech, kid speaking",
]
SINGING_LABELS = [
    "Singing",
    "Male singing",
    "Female singing",
    "Child singing",
    "Synthetic singing",
    "Yodeling",
    "Rapping",
]


def _resample_ratio(src_rate: int, dst_rate: int) -> tuple[int, int]:
    g = math.gcd(src_rate, dst_rate)
    return dst_rate // g, src_rate // g


class ClassifierState:
    """Per-session smoothing state.

    Kept outside VocalClassifier because one classifier instance is shared
    across sessions, and because carrying the EMA across chunk boundaries is
    what stops the attenuation from visibly jumping every chunk.
    """

    def __init__(self) -> None:
        self.ema: float | None = None
        self.last_singing: float = 0.0
        self.last_speech: float = 0.0


class VocalClassifier:
    def __init__(
        self,
        device: str = "cpu",
        window_seconds: float = 1.0,
        hop_seconds: float = 0.5,
        smoothing: float = 0.4,
    ):
        self.tagger = AudioTagging(device=device)
        self.window_seconds = window_seconds
        self.hop_seconds = hop_seconds
        # EMA weight for a new window's score; lower = smoother but slower to react.
        self.smoothing = smoothing

        import panns_inference.config as cfg

        self._speech_idx = [cfg.lb_to_ix[label] for label in SPEECH_LABELS if label in cfg.lb_to_ix]
        self._singing_idx = [cfg.lb_to_ix[label] for label in SINGING_LABELS if label in cfg.lb_to_ix]

    def gain_envelope(
        self,
        audio: np.ndarray,
        samplerate: int,
        strength: float = 1.0,
        state: ClassifierState | None = None,
    ) -> np.ndarray:
        """audio: (samples, channels) float32 at `samplerate`.

        Returns a (samples,) float32 gain curve in [0, 1] -- multiply it onto
        `audio` (broadcasting over channels) to duck singing-classified
        stretches while leaving speech-classified stretches at full gain.

        `strength` (0..1) is the UI slider: 0 is a no-op, 1 cuts detected
        singing as hard as the model's confidence allows.
        """
        n = len(audio)
        if strength <= 0.0:
            return np.ones(n, dtype=np.float32)

        mono = audio.mean(axis=1).astype(np.float32)
        up, down = _resample_ratio(samplerate, PANNS_SAMPLE_RATE)
        mono_32k = resample_poly(mono, up, down).astype(np.float32)

        window = int(self.window_seconds * PANNS_SAMPLE_RATE)
        hop = int(self.hop_seconds * PANNS_SAMPLE_RATE)
        if len(mono_32k) < window:
            return np.ones(n, dtype=np.float32)

        starts = list(range(0, len(mono_32k) - window + 1, hop))
        batch = np.stack([mono_32k[s : s + window] for s in starts], axis=0)

        with torch.no_grad():
            clipwise_output, _ = self.tagger.inference(batch)

        speech_score = clipwise_output[:, self._speech_idx].max(axis=1)
        singing_score = clipwise_output[:, self._singing_idx].max(axis=1)

        # Relative dominance rather than a raw difference: "Speech" fires on
        # singing too, so absolute margins stay small even for obvious singing.
        evidence = np.maximum(singing_score, speech_score)
        dominance = singing_score / (singing_score + speech_score + 1e-6)
        dominance = np.where(evidence < MIN_EVIDENCE, 0.0, dominance)

        # Smooth across windows *and* across chunk boundaries, so attenuation
        # doesn't wobble every chunk.
        smoothed = np.empty_like(dominance)
        ema = None if state is None else state.ema
        for i, value in enumerate(dominance):
            ema = value if ema is None else (self.smoothing * value + (1.0 - self.smoothing) * ema)
            smoothed[i] = ema
        if state is not None:
            state.ema = float(ema)
            state.last_singing = float(singing_score.mean())
            state.last_speech = float(speech_score.mean())

        # Higher strength => trigger sooner and ramp to full cut faster.
        threshold = 0.75 - 0.40 * strength
        ramp = max(0.05, (1.0 - threshold) * (1.0 - 0.8 * strength))
        excess = np.clip((smoothed - threshold) / ramp, 0.0, 1.0)
        window_gain = (1.0 - strength * excess).astype(np.float32)

        # Interpolate the gain curve straight onto the output sample grid.
        # Running it through resample_poly instead (as this used to) puts FIR
        # ringing on it: even an all-1.0 "don't attenuate" curve came back
        # dipping to ~0.76 at the edges, i.e. an audible level dip at every
        # chunk boundary. The curve is smooth and slowly varying, so linear
        # interpolation is both correct and artifact-free.
        window_centers_sec = np.array(
            [(s + window / 2) / PANNS_SAMPLE_RATE for s in starts], dtype=np.float64
        )
        output_positions_sec = np.arange(n, dtype=np.float64) / samplerate
        gain = np.interp(
            output_positions_sec,
            window_centers_sec,
            window_gain,
            left=window_gain[0],
            right=window_gain[-1],
        )
        return np.clip(gain, 0.0, 1.0).astype(np.float32)

    def apply(
        self,
        audio: np.ndarray,
        samplerate: int,
        strength: float = 1.0,
        state: ClassifierState | None = None,
    ) -> np.ndarray:
        """Convenience: returns `audio` with the gain envelope applied."""
        gain = self.gain_envelope(audio, samplerate, strength=strength, state=state)
        return audio * gain[:, None]
