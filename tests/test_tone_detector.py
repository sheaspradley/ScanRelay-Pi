"""
Tests for scanrelay.tone_detector.detect_lead_tone.

Uses synthetic PCM generated with only math and struct (no deps beyond stdlib).
Run with: python -m pytest tests/test_tone_detector.py -v
"""
from __future__ import annotations

import math
import random
import struct
import sys
from pathlib import Path

import pytest

# Make sure the package is importable from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from scanrelay.tone_detector import detect_lead_tone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000  # Hz


def _gen_sine(freq_hz: float, duration_s: float, amplitude: int = 20000) -> bytes:
    """Generate 16-bit LE mono PCM of a pure sine wave."""
    n_samples = int(SAMPLE_RATE * duration_s)
    samples = [
        int(amplitude * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def _gen_noise(duration_s: float, amplitude: int = 15000, seed: int = 42) -> bytes:
    """Generate 16-bit LE mono PCM that mimics speech via voiced/unvoiced alternation.

    Real speech alternates rapidly between:
      - voiced segments (fundamental ~80-300 Hz, low ZCR like a sine)
      - unvoiced fricatives / silence (broadband noise, very high ZCR)

    This produces HIGH ZCR variance across 100 ms frames, which the
    tone detector should correctly reject as non-tone audio.

    Note: pure Gaussian white noise is NOT a good test here because it has
    nearly constant ZCR ≈ 0.5 per frame (every adjacent sample pair flips sign
    about half the time), making it look like a stable signal to a ZCR-variance
    detector. Real speech, unlike white noise, has irregular voicing cycles.
    """
    rng = random.Random(seed)
    frame_samples = int(SAMPLE_RATE * 0.1)  # 100 ms frames
    n_frames = int(duration_s * 10)         # 10 frames per second
    samples: list[int] = []
    for frame_idx in range(n_frames):
        if frame_idx % 3 < 2:  # voiced (2/3 of frames): low ZCR, sine-like
            freq = rng.uniform(80, 300)
            voiced = [
                int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
                for i in range(frame_samples)
            ]
            samples.extend(voiced)
        else:  # unvoiced (1/3 of frames): broadband, high ZCR
            unvoiced = [
                max(-32767, min(32767, int(rng.gauss(0, amplitude / 3))))
                for _ in range(frame_samples)
            ]
            samples.extend(unvoiced)
    # Trim/pad to exact sample count.
    n_samples = int(SAMPLE_RATE * duration_s)
    samples = (samples + [0] * n_samples)[:n_samples]
    return struct.pack(f"<{n_samples}h", *samples)


def _gen_structured_noise(duration_s: float, amplitude: int = 15000, seed: int = 99) -> bytes:
    """Generate speech-like noise: low-frequency modulated Gaussian noise.

    This mimics speech more closely than pure white noise because it has
    amplitude modulation (like voiced/unvoiced transitions) while keeping
    irregular zero-crossing rates.
    """
    rng = random.Random(seed)
    n_samples = int(SAMPLE_RATE * duration_s)
    samples = []
    for i in range(n_samples):
        # Envelope modulation at ~4 Hz (syllable rate)
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 4 * i / SAMPLE_RATE)
        s = int(env * rng.gauss(0, amplitude / 3))
        samples.append(max(-32767, min(32767, s)))
    return struct.pack(f"<{n_samples}h", *samples)


# ---------------------------------------------------------------------------
# Test 1: pure 1 kHz sine for 5s → should detect tone ending at > 4.5s
# ---------------------------------------------------------------------------

def test_pure_sine_detected():
    """A 5-second 1 kHz sine wave must be flagged as a lead tone."""
    pcm = _gen_sine(1000.0, 5.0)
    end, conf = detect_lead_tone(pcm, SAMPLE_RATE, window_seconds=8.0)
    assert end > 4.5, (
        f"Expected tone_end > 4.5s for pure 1 kHz sine, got {end:.2f}s (conf={conf:.2f})"
    )
    assert conf > 0.0, "Confidence should be non-zero when tone detected"


# ---------------------------------------------------------------------------
# Test 2: random Gaussian noise → should NOT detect tone
# ---------------------------------------------------------------------------

def test_noise_not_detected():
    """Speech-like noise (voiced/unvoiced alternation) must NOT be flagged as a tone.

    The noise generator produces high ZCR variance across frames (like real speech),
    which the detector should correctly reject.
    """
    pcm = _gen_noise(5.0)
    end, conf = detect_lead_tone(pcm, SAMPLE_RATE, window_seconds=8.0)
    assert end == 0.0, (
        f"Expected no tone detection for speech-like noise, got tone_end={end:.2f}s (conf={conf:.2f})"
    )
    # Confidence must also be 0 when no tone is detected.
    assert conf == 0.0, f"Confidence should be 0.0 when no tone detected, got {conf:.2f}"


# ---------------------------------------------------------------------------
# Test 3: 3s sine + 5s speech-like noise → tone should end around 3s (±0.5s)
# ---------------------------------------------------------------------------

def test_sine_then_speech_boundary():
    """Tone followed by speech: end timestamp should be near the boundary."""
    tone_pcm   = _gen_sine(1000.0, 3.0)
    speech_pcm = _gen_structured_noise(5.0)
    combined   = tone_pcm + speech_pcm

    end, conf = detect_lead_tone(combined, SAMPLE_RATE, window_seconds=8.0)

    assert end > 0.0, (
        "Expected a tone to be detected when 3s sine precedes speech, "
        f"got end={end:.2f}s"
    )
    assert 2.5 <= end <= 3.5, (
        f"Expected tone end near 3.0s (±0.5s), got {end:.2f}s (conf={conf:.2f})"
    )


# ---------------------------------------------------------------------------
# Test 4: silence / very short clip → no crash, returns (0.0, 0.0)
# ---------------------------------------------------------------------------

def test_empty_pcm():
    """Empty PCM must return (0.0, 0.0) without raising."""
    end, conf = detect_lead_tone(b"", SAMPLE_RATE)
    assert (end, conf) == (0.0, 0.0)


def test_very_short_clip():
    """A clip shorter than 2 analysis frames must return (0.0, 0.0)."""
    # 50 ms of sine — shorter than _MIN_TONE_FRAMES * _FRAME_MS (200 ms)
    pcm = _gen_sine(1000.0, 0.05)
    end, conf = detect_lead_tone(pcm, SAMPLE_RATE)
    assert (end, conf) == (0.0, 0.0)
