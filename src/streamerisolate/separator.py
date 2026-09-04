"""Wraps a pretrained Demucs model to isolate speech (the 'vocals' stem) from a
chunk of audio, treating everything else (music, crowd, game SFX) as background
to be removed. This is the "speech vs. non-speech" approximation described in
the project's v1 scope, not per-instrument separation.
"""

from __future__ import annotations

import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class SpeechIsolator:
    def __init__(self, model_name: str = "htdemucs", device: str | None = None):
        self.device = device or pick_device()
        self.model = get_model(model_name)
        self.model.to(self.device)
        self.model.eval()
        self.samplerate: int = self.model.samplerate
        self.audio_channels: int = self.model.audio_channels
        self._vocals_index = self.model.sources.index("vocals")

    @torch.no_grad()
    def isolate_speech(self, chunk: torch.Tensor) -> torch.Tensor:
        """chunk: (channels, samples) float32 tensor at self.samplerate.
        Returns a (channels, samples) tensor containing only the 'vocals' stem.
        """
        if chunk.dim() != 2:
            raise ValueError(f"expected (channels, samples), got shape {tuple(chunk.shape)}")

        batch = chunk.unsqueeze(0).to(self.device)
        stems = apply_model(self.model, batch, device=self.device, progress=False)[0]
        vocals = stems[self._vocals_index]
        return vocals.cpu()
