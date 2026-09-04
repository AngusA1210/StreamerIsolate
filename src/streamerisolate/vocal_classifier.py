"""Reduces song-vocal bleed-through in the Demucs 'vocals' stem.

Demucs's vocals stem separates singing-like content from instrumentals, but
it has no notion of "the streamer talking" vs. "a song's singing" -- both
are vocal-like and land in the same stem. This module runs a pretrained
general-purpose audio tagger (PANNs Cnn14, trained on AudioSet) over short
windows of the isolated vocals and attenuates windows where singing
confidently outscores speech, producing a smooth per-sample gain envelope
rather than a hard on/off switch (to avoid clicky artifacts at boundaries).

It's a soft mitigation, not a fix: enthusiastic speech can still read as
singing-ish, and quiet/subdued singing can still read as speech-ish. See the
project README for the known limitation this addresses.
"""

from __future__ import annotations

import math

import numpy as np
import torch
from panns_inference import AudioTagging
from scipy.signal import resample_poly

PANNS_SAMPLE_RATE = 32000

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
]


def _resample_ratio(src_rate: int, dst_rate: int) -> tuple[int, int]:
    g = math.gcd(src_rate, dst_rate)
    return dst_rate // g, src_rate // g


class VocalClassifier:
    def __init__(
        self,
        device: str = "cpu",
        window_seconds: float = 1.0,
        hop_seconds: float = 0.5,
        min_gain: float = 0.12,
        margin_for_full_cut: float = 0.3,
    ):
        self.tagger = AudioTagging(device=device)
        self.window_seconds = window_seconds
        self.hop_seconds = hop_seconds
        self.min_gain = min_gain
        self.margin_for_full_cut = margin_for_full_cut

        import panns_inference.config as cfg

        self._speech_idx = [cfg.lb_to_ix[label] for label in SPEECH_LABELS]
        self._singing_idx = [cfg.lb_to_ix[label] for label in SINGING_LABELS]

    def gain_envelope(self, audio: np.ndarray, samplerate: int, min_gain: float | None = None) -> np.ndarray:
        """audio: (samples, channels) float32 at `samplerate`.
        Returns a (samples,) float32 gain curve in [min_gain, 1.0] -- multiply
        it onto `audio` (broadcasting over channels) to attenuate singing-
        classified stretches while leaving speech-classified stretches at
        full gain.

        `min_gain` overrides the instance default for this call, so a live
        strength control can vary it per chunk without mutating shared state.
        """
        if min_gain is None:
            min_gain = self.min_gain
        n = len(audio)
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

        # Only attenuate when singing confidently beats speech -- bias
        # toward keeping speech over aggressively cutting singing, since a
        # false cut (losing real speech) is worse for this use case than a
        # false pass (some song vocal bleeding through).
        margin = singing_score - speech_score
        window_gain = np.clip(1.0 - (margin / self.margin_for_full_cut), min_gain, 1.0).astype(np.float32)

        window_centers = np.array([s + window / 2 for s in starts], dtype=np.float64)
        sample_positions = np.arange(len(mono_32k), dtype=np.float64)
        gain_32k = np.interp(
            sample_positions, window_centers, window_gain, left=window_gain[0], right=window_gain[-1]
        ).astype(np.float32)

        up_back, down_back = _resample_ratio(PANNS_SAMPLE_RATE, samplerate)
        gain_target_rate = resample_poly(gain_32k, up_back, down_back).astype(np.float32)
        gain_target_rate = np.clip(gain_target_rate, min_gain, 1.0)

        if len(gain_target_rate) < n:
            gain_target_rate = np.pad(gain_target_rate, (0, n - len(gain_target_rate)), mode="edge")
        return gain_target_rate[:n]

    def apply(self, audio: np.ndarray, samplerate: int, min_gain: float | None = None) -> np.ndarray:
        """Convenience: returns `audio` with the gain envelope applied."""
        gain = self.gain_envelope(audio, samplerate, min_gain=min_gain)
        return audio * gain[:, None]

    @staticmethod
    def min_gain_for_strength(strength: float) -> float:
        """Map a 0..1 UI 'strength' onto the attenuation floor.

        strength 0 -> min_gain 1.0 (nothing attenuated, classifier is a no-op)
        strength 1 -> min_gain 0.0 (singing cut as hard as the model allows)
        """
        return float(np.clip(1.0 - strength, 0.0, 1.0))
