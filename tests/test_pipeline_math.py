"""Tests for the chunking, resampling and gain maths.

These are the parts that have actually produced bugs -- chunk/hop arithmetic,
and a gain envelope that once picked up filter ringing and dipped ~24% at
every chunk edge. They're pure functions of numpy arrays, so they run without
audio hardware; the model-dependent behaviour is covered separately in
test_classifier.py, which is skipped when the checkpoints aren't present.
"""

from __future__ import annotations

import numpy as np
import pytest

from streamerisolate.server import _resample_ratio


class FakeIsolator:
    """Stands in for Demucs: same interface, passes audio through unchanged."""

    samplerate = 44100
    audio_channels = 2

    def isolate_speech(self, chunk):
        return chunk


def test_resample_ratio_reduces_to_lowest_terms():
    assert _resample_ratio(48000, 44100) == (147, 160)
    assert _resample_ratio(44100, 48000) == (160, 147)


def test_resample_ratio_is_identity_for_equal_rates():
    assert _resample_ratio(44100, 44100) == (1, 1)


@pytest.mark.parametrize(
    "chunk_seconds,overlap_seconds,input_seconds,expected_chunks",
    [
        # Output only starts once a full chunk has accumulated, then advances
        # one hop at a time: floor((input - chunk) / hop) + 1.
        (3.0, 0.75, 5.0, 1),
        (3.0, 0.75, 8.0, 3),
        (2.0, 0.5, 5.0, 3),
        (3.0, 0.75, 2.0, 0),  # less than one chunk in: nothing out yet
    ],
)
def test_stream_session_emits_expected_amount(
    chunk_seconds, overlap_seconds, input_seconds, expected_chunks
):
    from streamerisolate.server import StreamSession

    browser_rate = 48000
    session = StreamSession(
        isolator=FakeIsolator(),
        browser_rate=browser_rate,
        channels=2,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
        vocal_classifier=None,
    )

    frames = int(input_seconds * browser_rate)
    audio = np.zeros((frames, 2), dtype=np.float32)
    blocks = session.feed(audio.tobytes())

    assert len(blocks) == expected_chunks

    if expected_chunks:
        hop_seconds = chunk_seconds - overlap_seconds
        total_frames = sum(len(b) // 4 // 2 for b in blocks)
        expected_frames = expected_chunks * hop_seconds * browser_rate
        # Resampling to the model rate and back can shift this a frame or two.
        assert abs(total_frames - expected_frames) < browser_rate * 0.01


def test_stream_session_rejects_overlap_at_least_chunk():
    from streamerisolate.server import StreamSession

    with pytest.raises(ValueError):
        StreamSession(
            isolator=FakeIsolator(),
            browser_rate=48000,
            channels=2,
            chunk_seconds=1.0,
            overlap_seconds=1.0,
        )


def test_feeding_in_small_packets_matches_one_big_feed():
    """The websocket delivers audio in ~20ms packets; the chunking must not
    depend on how the input happens to be sliced."""
    from streamerisolate.server import StreamSession

    browser_rate = 48000
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal((browser_rate * 5, 2)) * 0.1).astype(np.float32)

    def run(packet_frames):
        session = StreamSession(
            isolator=FakeIsolator(),
            browser_rate=browser_rate,
            channels=2,
            chunk_seconds=3.0,
            overlap_seconds=0.75,
            vocal_classifier=None,
        )
        out = []
        for start in range(0, len(audio), packet_frames):
            out += session.feed(audio[start : start + packet_frames].tobytes())
        return b"".join(out)

    assert run(len(audio)) == run(960)


def test_pipeline_hop_arithmetic():
    from streamerisolate.pipeline import Pipeline

    pipeline = Pipeline(
        output_device=0,
        isolator=FakeIsolator(),
        input_device=0,
        chunk_seconds=3.0,
        overlap_seconds=0.75,
    )
    assert pipeline.chunk_samples == int(3.0 * 44100)
    assert pipeline.overlap_samples == int(0.75 * 44100)
    assert pipeline.hop_samples == pipeline.chunk_samples - pipeline.overlap_samples


def test_pipeline_requires_exactly_one_input_source():
    from streamerisolate.pipeline import Pipeline

    with pytest.raises(ValueError):
        Pipeline(output_device=0, isolator=FakeIsolator())  # neither
    with pytest.raises(ValueError):
        Pipeline(  # both
            output_device=0,
            isolator=FakeIsolator(),
            input_device=0,
            capture_app="Chrome",
        )
