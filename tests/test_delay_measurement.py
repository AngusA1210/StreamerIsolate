"""The delay the pipeline reports must track the real input-to-output delay.

The Firefox extension holds its video back by this figure, and every change
to it makes the picture freeze or skip. An earlier estimate (chunk length +
processing time + queue depth) swung by a whole hop under ordinary timing
jitter while the real delay stayed constant; with the GPU shared with another
app that meant a visible video hitch about twice a second.

The pipeline is driven here in virtual time -- its own callbacks, no audio
devices or threads -- with a ramp as input so every output sample identifies
the input position it came from, independently of the pipeline's counters.
"""

from __future__ import annotations

import numpy as np
import pytest

from streamerisolate.pipeline import Pipeline

RETARGET_THRESHOLD = 0.15  # seconds; the extension ignores smaller changes


class FakeIsolator:
    samplerate = 44100
    audio_channels = 2

    def isolate_speech(self, chunk):
        return chunk


def run(seconds, process_seconds, seed=0):
    """Returns (reported, true) delay sampled every 0.5s once audio is playing."""
    rng = np.random.default_rng(seed)
    pipeline = Pipeline(output_device=0, isolator=FakeIsolator(), input_device=0)
    rate, block = pipeline.samplerate, pipeline.block_size

    captured = 0
    played = 0
    pending = None  # the chunk the simulated worker is "processing"
    ready_at = 0.0
    next_sample = 0.0
    reported, true = [], []

    for i in range(int(seconds * rate / block)):
        now = i * block / rate

        positions = np.arange(captured + 1, captured + block + 1, dtype=np.float64)
        pipeline._input_callback(np.stack([positions, positions], axis=1), block, None, None)
        captured += block

        if pending is not None and now >= ready_at:
            pipeline._output_queue.put(pending)
            pipeline.chunks_emitted += 1
            pending = None
        if pending is None and len(pipeline._input_buffer) >= pipeline.chunk_samples:
            pending = pipeline._input_buffer[: pipeline.hop_samples].copy()
            pipeline._input_buffer = pipeline._input_buffer[pipeline.hop_samples :]
            took = process_seconds(rng)
            pipeline._last_process_seconds = took
            ready_at = now + took

        out = np.zeros((block, 2), dtype=np.float64)
        pipeline._output_callback(out, block, None, None)
        heard = out[:, 0][out[:, 0] > 0]
        if len(heard):
            played = int(heard[-1])

        if now >= next_sample:
            next_sample += 0.5
            if played:
                reported.append(pipeline.delay_seconds)
                true.append((captured - played) / rate)

    return np.array(reported), np.array(true)


SCENARIOS = {
    "steady": lambda rng: 0.54,
    "jittery": lambda rng: 0.54 + rng.uniform(-0.15, 0.15),
    "gpu shared with another app": lambda rng: 1.5 if rng.random() < 0.08 else 0.54 + rng.uniform(-0.1, 0.1),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_reported_delay_matches_the_real_delay(name):
    reported, true = run(120, SCENARIOS[name])
    assert len(reported) > 100
    assert np.abs(reported - true).max() < 0.05


@pytest.mark.parametrize("name", SCENARIOS)
def test_delay_is_steady_once_playback_settles(name):
    """After the worst processing hiccup has been absorbed the real delay
    stops changing, so the reported one must too -- no video retargets."""
    reported, _ = run(120, SCENARIOS[name])
    settled = reported[len(reported) // 2 :]
    assert settled.max() - settled.min() < RETARGET_THRESHOLD


def test_delay_before_first_audio_is_the_expected_wait():
    pipeline = Pipeline(output_device=0, isolator=FakeIsolator(), input_device=0)
    assert pipeline.delay_seconds == pytest.approx(pipeline.chunk_seconds)
