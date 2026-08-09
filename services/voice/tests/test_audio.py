"""Verify the numpy codec matches audioop exactly, then verify the pipeline.

audioop is gone in Python 3.13. While it still exists we use it as ground
truth; once it's gone these tests keep passing against the frozen tables.
"""
import numpy as np
import pytest

from app import audio

try:
    import audioop
    HAVE_AUDIOOP = True
except ImportError:
    HAVE_AUDIOOP = False


@pytest.mark.skipif(not HAVE_AUDIOOP, reason="audioop removed in 3.13+")
def test_decode_matches_audioop_for_all_256_bytes():
    raw = bytes(range(256))
    ours = audio.ulaw_to_pcm16(raw)
    theirs = np.frombuffer(audioop.ulaw2lin(raw, 2), dtype=np.int16)
    assert np.array_equal(ours, theirs)


@pytest.mark.skipif(not HAVE_AUDIOOP, reason="audioop removed in 3.13+")
def test_encode_matches_audioop_across_full_int16_range():
    samples = np.arange(-32768, 32768, dtype=np.int16)
    ours = audio.pcm16_to_ulaw(samples)
    theirs = audioop.lin2ulaw(samples.tobytes(), 2)
    assert ours == theirs


def test_round_trip_preserves_signal_shape():
    """mu-law is lossy, but a sine must survive with high correlation."""
    t = np.linspace(0, 1, 8000, endpoint=False)
    original = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    decoded = audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(original))
    assert np.corrcoef(original, decoded)[0, 1] > 0.999


def test_phone_to_model_doubles_sample_count():
    payload = audio.pcm16_to_ulaw(np.zeros(160, dtype=np.int16))
    out = np.frombuffer(audio.phone_to_model(payload, 16000), dtype=np.int16)
    assert len(out) == 320


def test_model_to_phone_downsamples_24k_to_8k():
    pcm = np.zeros(2400, dtype=np.int16).tobytes()
    assert len(audio.model_to_phone(pcm, 24000)) == 800


def test_silence_stays_silent_through_the_full_loop():
    """Regression guard: a DC offset bug here becomes audible hiss on the call."""
    silence = np.zeros(1600, dtype=np.int16)
    out = audio.ulaw_to_pcm16(audio.model_to_phone(silence.tobytes(), 16000))
    assert np.abs(out).max() <= 8


def test_frames_splits_into_20ms_chunks_with_remainder():
    chunks, rest = audio.frames(b"\xff" * 400)
    assert len(chunks) == 2 and all(len(c) == 160 for c in chunks)
    assert len(rest) == 80


def test_frames_handles_exact_multiple():
    chunks, rest = audio.frames(b"\x00" * 320)
    assert len(chunks) == 2 and rest == b""
