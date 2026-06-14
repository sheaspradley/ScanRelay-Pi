"""
Lead-tone detector for EMS/fire dispatch audio.

Heuristic: pure stdlib, no external dependencies.

Algorithm
---------
EMS/fire two-tone pages and alerting tones have three distinctive properties
compared to speech:

1. **High energy** — tones are transmitted at full transmitter power; speech
   tends to have more variance.
2. **Low zero-crossing-rate (ZCR) variance across frames** — a sine wave crosses
   zero exactly 2 * frequency times per second, very regularly; speech ZCR
   fluctuates wildly because it mixes voiced / unvoiced / silence.
3. **Low energy variance across frames** — a continuous tone stays loud; speech
   alternates between loud voiced segments and quieter unvoiced/pause segments.

We analyse the *first* ``window_seconds`` of each clip in 100 ms frames,
classify each frame as "tone-like" or not, then look for a run of tone-like
frames at the very start of the clip.  We stop as soon as we hit a frame that
breaks the pattern, which means real speech that happens to follow a tone is
**never** trimmed.

This is intentionally conservative: we only flag clear, loud, spectrally-stable
signals.  When in doubt we return (0.0, 0.0) so the full clip is transcribed.

Public API
----------
    detect_lead_tone(pcm, sample_rate, window_seconds=8.0) -> (tone_end_s, confidence)

    * tone_end_s  — seconds from the start of the clip where the tone ends.
                    0.0 means "no tone detected".
    * confidence  — 0.0 – 1.0; proportion of leading frames that looked tone-like.
                    Meaningful only when tone_end_s > 0.
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRAME_MS = 100          # analysis frame length in milliseconds
_MIN_TONE_FRAMES = 2     # need at least this many consecutive tone frames at start
_ZCR_VAR_THRESHOLD = 0.15  # max normalised ZCR variance to call a frame "tone-like"
_ENERGY_VAR_THRESHOLD = 0.20  # max normalised energy variance across leading frames
_MIN_RMS_LINEAR = 10.0   # ~-68 dBFS (S16 range 32768): ignore silence frames


def _rms(samples: list[int]) -> float:
    """Root-mean-square of a list of signed 16-bit samples."""
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _zcr(samples: list[int]) -> float:
    """Zero-crossing rate: fraction of consecutive-sample sign changes."""
    if len(samples) < 2:
        return 0.0
    crossings = sum(
        1 for i in range(1, len(samples))
        if (samples[i] >= 0) != (samples[i - 1] >= 0)
    )
    return crossings / (len(samples) - 1)


def _variance(values: list[float]) -> float:
    """Population variance of a list of floats."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _norm_variance(values: list[float]) -> float:
    """Variance normalised by the squared mean (coefficient of variation²).

    Returns 0 when mean is near zero to avoid division errors.
    """
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if abs(mean) < 1e-9:
        return 0.0
    return _variance(values) / (mean * mean)


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def detect_lead_tone(
    pcm: bytes,
    sample_rate: int,
    window_seconds: float = 8.0,
) -> tuple[float, float]:
    """Detect and measure a leading dispatch tone in raw 16-bit LE mono PCM.

    Parameters
    ----------
    pcm:            Raw PCM bytes, 16-bit signed little-endian, mono.
    sample_rate:    Samples per second (must match the PCM data, typically 16000).
    window_seconds: How many seconds at the start to examine.  Anything after
                    this point is ignored even if it looks like a tone.

    Returns
    -------
    (tone_end_seconds, confidence)
        tone_end_seconds — where the tone region ends; 0.0 if no tone found.
        confidence       — fraction of leading frames that matched the tone
                           signature (0.0 – 1.0); 0.0 when no tone detected.

    Notes
    -----
    Conservative by design: ambiguous or mixed frames stop the tone region.
    Speech that immediately follows a tone is never included in the trim window.
    """
    frame_samples = int(sample_rate * _FRAME_MS / 1000)
    frame_bytes   = frame_samples * 2  # 2 bytes per S16 sample
    max_frames    = int(window_seconds * 1000 / _FRAME_MS)

    # How many complete frames fit in the PCM?
    total_frames = min(len(pcm) // frame_bytes, max_frames)
    if total_frames < _MIN_TONE_FRAMES:
        return (0.0, 0.0)

    frame_rms: list[float] = []
    frame_zcr: list[float] = []

    for i in range(total_frames):
        chunk = pcm[i * frame_bytes : (i + 1) * frame_bytes]
        n = len(chunk) // 2
        samples = list(struct.unpack_from(f"<{n}h", chunk))
        frame_rms.append(_rms(samples))
        frame_zcr.append(_zcr(samples))

    # Walk from the start: extend the tone region as long as each frame
    # looks tone-like relative to what we've seen so far.
    tone_frame_count = 0
    for i in range(total_frames):
        r = frame_rms[i]
        z = frame_zcr[i]

        # Must be louder than the silence floor.
        if r < _MIN_RMS_LINEAR:
            break

        # Check ZCR stability: the running ZCR should have low normalised variance.
        zcrs_so_far = frame_zcr[: i + 1]
        if _norm_variance(zcrs_so_far) > _ZCR_VAR_THRESHOLD:
            break

        # Check energy stability: the running energy should stay relatively flat.
        rms_so_far = frame_rms[: i + 1]
        if _norm_variance(rms_so_far) > _ENERGY_VAR_THRESHOLD:
            break

        tone_frame_count += 1

    if tone_frame_count < _MIN_TONE_FRAMES:
        return (0.0, 0.0)

    tone_end_seconds = tone_frame_count * _FRAME_MS / 1000.0
    confidence = tone_frame_count / total_frames
    return (tone_end_seconds, confidence)


# ---------------------------------------------------------------------------
# CLI — ad-hoc testing on a real WAV file
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import wave

    if len(sys.argv) < 2:
        print("Usage: python -m scanrelay.tone_detector <file.wav>", file=sys.stderr)
        sys.exit(1)

    wav_path = Path(sys.argv[1])
    if not wav_path.exists():
        print(f"File not found: {wav_path}", file=sys.stderr)
        sys.exit(1)

    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    print(f"WAV: {wav_path.name}  rate={sr}  channels={ch}  width={sw}")

    # Downmix to mono if needed
    if ch == 2 and sw == 2:
        n = len(raw) // 4
        pairs = struct.unpack_from(f"<{n * 2}h", raw)
        mono = struct.pack(f"<{n}h", *(
            (pairs[i * 2] + pairs[i * 2 + 1]) // 2 for i in range(n)
        ))
        raw = mono

    window = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    end_s, conf = detect_lead_tone(raw, sr, window_seconds=window)

    if end_s > 0:
        print(f"Tone detected: ends at {end_s:.2f}s  confidence={conf:.2f}")
    else:
        print("No lead tone detected.")
