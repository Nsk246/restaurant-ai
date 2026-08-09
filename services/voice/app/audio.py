"""G.711 mu-law codec and PSTN resampling.

Twilio Media Streams carry 8 kHz mu-law. Realtime speech models want 16 kHz
PCM16 in and emit 16 or 24 kHz PCM16 out. This module is the only place that
knows about either fact.

Why not `audioop`: it was removed from the standard library in Python 3.13.
The lookup tables here are built at import time with numpy and are verified
byte-for-byte against `audioop` in the test suite while it still exists, so
we get the same output with no deprecated dependency.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> np.ndarray:
    """256 mu-law bytes -> int16 PCM."""
    u = np.arange(256, dtype=np.int32)
    v = ~u & 0xFF
    mantissa = v & 0x0F
    exponent = (v & 0x70) >> 4
    magnitude = ((mantissa << 3) + _BIAS) << exponent
    magnitude -= _BIAS
    sample = np.where(v & 0x80, -magnitude, magnitude)
    return sample.astype(np.int16)


def _build_encode_table() -> np.ndarray:
    """Full int16 range -> mu-law byte. 64K entries, built once.

    The `abs(s >> 2) << 2` is not decoration. G.711 is defined over a 14-bit
    domain, so the low two bits are discarded before the magnitude is taken.
    Because the shift is arithmetic, negative samples round away from zero,
    which is why a naive `abs(s)` disagrees with the reference codec on 381
    values, all of them negative. Verified against audioop in the tests.
    """
    s = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(s < 0, 0x80, 0x00).astype(np.int32)
    mag = np.minimum(np.abs(s >> 2) << 2, _CLIP) + _BIAS

    # Exponent is the position of the highest set bit above bit 7.
    exponent = np.zeros_like(mag)
    for e in range(1, 8):
        exponent = np.where(mag >= (1 << (e + 7)), e, exponent)

    mantissa = (mag >> (exponent + 3)) & 0x0F
    byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return byte.astype(np.uint8)


_DECODE = _build_decode_table()
_ENCODE = _build_encode_table()


def ulaw_to_pcm16(payload: bytes) -> np.ndarray:
    """Twilio mu-law bytes -> 8 kHz PCM16 samples."""
    return _DECODE[np.frombuffer(payload, dtype=np.uint8)]


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    """8 kHz PCM16 samples -> Twilio mu-law bytes."""
    idx = samples.astype(np.int32) + 32768
    return _ENCODE[np.clip(idx, 0, 65535)].tobytes()


def resample(samples: np.ndarray, src_hz: int, dst_hz: int) -> np.ndarray:
    """Polyphase resample. Exact ratios only, which is all telephony needs."""
    if src_hz == dst_hz:
        return samples
    from math import gcd

    g = gcd(src_hz, dst_hz)
    out = signal.resample_poly(samples, up=dst_hz // g, down=src_hz // g)
    return np.clip(out, -32768, 32767).astype(np.int16)


def phone_to_model(payload: bytes, model_hz: int = 16000) -> bytes:
    """Inbound: Twilio mu-law 8k -> model PCM16."""
    return resample(ulaw_to_pcm16(payload), 8000, model_hz).tobytes()


def model_to_phone(pcm: bytes, model_hz: int = 24000) -> bytes:
    """Outbound: model PCM16 -> Twilio mu-law 8k."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    return pcm16_to_ulaw(resample(samples, model_hz, 8000))


# Twilio expects 20 ms frames: 160 mu-law bytes at 8 kHz.
FRAME_BYTES = 160


def frames(payload: bytes, size: int = FRAME_BYTES):
    """Split into whole frames; return the frames and any trailing remainder.

    Twilio tolerates larger writes, but pacing in 20 ms frames keeps barge-in
    responsive: a `clear` lands between frames instead of after a long blob.
    """
    n = len(payload) // size
    return [payload[i * size : (i + 1) * size] for i in range(n)], payload[n * size :]
