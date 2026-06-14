"""
ALSA audio capture from the scanner's USB sound card.

Uses arecord under the hood (no PyAudio/portaudio dependency hell on Bookworm).
We KEEP the captured PCM this time — it gets fed to whisper.cpp for transcription
when VAD signals end-of-transmission.
"""
from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator

from .config import AudioConfig

log = logging.getLogger(__name__)


def frame_bytes(cfg: AudioConfig) -> int:
    """Bytes per VAD frame: samples * 2 (16-bit) * channels."""
    samples = cfg.sample_rate * cfg.frame_ms // 1000
    return samples * 2 * cfg.channels


def capture_frames(cfg: AudioConfig) -> Iterator[bytes]:
    """
    Yield raw 16-bit LE PCM frames of exactly `frame_bytes(cfg)` length.

    Runs arecord as a subprocess and reads stdout in fixed chunks. Caller is
    responsible for restarting on RuntimeError.
    """
    cmd = [
        "arecord",
        "-D", cfg.device,
        "-f", "S16_LE",
        "-c", str(cfg.channels),
        "-r", str(cfg.sample_rate),
        "-t", "raw",
        "-q",
    ]
    log.info("Starting arecord: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
    )
    if proc.stdout is None:
        raise RuntimeError("arecord stdout unavailable")

    fb = frame_bytes(cfg)
    try:
        while True:
            chunk = proc.stdout.read(fb)
            if not chunk:
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise RuntimeError(f"arecord ended unexpectedly: {err}")
            if len(chunk) < fb:
                chunk = chunk + b"\x00" * (fb - len(chunk))
            yield chunk
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def list_input_devices() -> str:
    """Dump `arecord -l` output for troubleshooting USB sound card detection."""
    out = subprocess.run(
        ["arecord", "-l"], capture_output=True, text=True, check=False
    )
    return out.stdout + out.stderr
