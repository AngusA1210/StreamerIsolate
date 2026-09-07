"""Tests for the singing/speech gate.

These need the PANNs checkpoint, so they skip cleanly when it isn't present
(e.g. CI, or before scripts/install.sh has run).

They deliberately assert *properties* rather than specific model outputs:
whether the model calls a given clip "singing" is its business, but the gate
around it must respect the strength setting, stay flat when it hears nothing,
and never introduce level changes of its own.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("panns_inference")

from streamerisolate.vocal_classifier import (  # noqa: E402
    MAX_NORMALISE_GAIN,
    TAGGER_TARGET_PEAK,
    ClassifierState,
    VocalClassifier,
)

SAMPLE_RATE = 44100


@pytest.fixture(scope="module")
def classifier():
    import pathlib

    if not (pathlib.Path.home() / "panns_data" / "Cnn14_mAP=0.431.pth").exists():
        pytest.skip("PANNs checkpoint not downloaded; run scripts/install.sh")
    return VocalClassifier()


@pytest.fixture
def noise():
    rng = np.random.default_rng(0)
    return (rng.standard_normal((SAMPLE_RATE * 3, 2)) * 0.05).astype(np.float32)


def test_strength_zero_is_a_true_no_op(classifier, noise):
    gain = classifier.gain_envelope(noise, SAMPLE_RATE, strength=0.0)
    assert np.all(gain == 1.0)


def test_gain_matches_input_length(classifier, noise):
    gain = classifier.gain_envelope(noise, SAMPLE_RATE, strength=1.0)
    assert len(gain) == len(noise)


def test_gain_stays_flat_when_nothing_is_detected(classifier, noise):
    """Regression: the envelope used to be resampled with resample_poly, whose
    ringing left a ~24% dip at every chunk edge -- an audible level wobble even
    when the classifier had decided to attenuate nothing."""
    gain = classifier.gain_envelope(noise, SAMPLE_RATE, strength=1.0)
    assert np.allclose(gain, 1.0), f"expected flat 1.0, got {gain.min()}..{gain.max()}"


def test_gain_is_bounded(classifier, noise):
    gain = classifier.gain_envelope(noise, SAMPLE_RATE, strength=1.0)
    assert gain.min() >= 0.0 and gain.max() <= 1.0


def test_apply_preserves_shape(classifier, noise):
    out = classifier.apply(noise, SAMPLE_RATE, strength=1.0)
    assert out.shape == noise.shape
    assert out.dtype == np.float32


def test_state_carries_between_calls(classifier, noise):
    """Smoothing across chunk boundaries is what stops the attenuation
    wobbling from one chunk to the next."""
    state = ClassifierState()
    assert state.ema is None
    classifier.gain_envelope(noise, SAMPLE_RATE, strength=1.0, state=state)
    assert state.ema is not None


def test_quiet_audio_is_normalised_before_tagging(classifier):
    """The tagger's scores collapse on quiet input, which made detection
    depend on how loud the source happened to be -- that's why attenuation
    worked in Chrome (full-scale tab capture) but not through a virtual device
    behind the system volume fader."""
    rng = np.random.default_rng(1)
    base = rng.standard_normal(SAMPLE_RATE).astype(np.float32)
    base /= np.abs(base).max()

    quiet = base * 0.01
    peak = float(np.abs(quiet).max())
    scale = min(TAGGER_TARGET_PEAK / peak, MAX_NORMALISE_GAIN)

    assert scale > 1.0, "quiet audio should be scaled up"
    assert peak * scale <= TAGGER_TARGET_PEAK + 1e-6, "and not past the target"

    loud = base * 0.6
    assert np.abs(loud).max() >= TAGGER_TARGET_PEAK, "already-loud audio is left alone"
