"""
Transcribe captured PCM with whisper.cpp.

We shell out to the whisper.cpp `main` binary rather than using a Python binding,
because it's the most reliable path on Raspberry Pi OS Bookworm and gives us
control over threads/timeouts. Audio is written to a temp WAV; transcript is
read back from stdout.

Build whisper.cpp once:
    git clone https://github.com/ggerganov/whisper.cpp /opt/whisper.cpp
    cd /opt/whisper.cpp && make -j4
    bash ./models/download-ggml-model.sh base.en-q5_1
"""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import WhisperConfig

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    text: str
    duration_audio_seconds: float
    duration_compute_seconds: float


def _write_wav(pcm: bytes, sample_rate: int, path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def transcribe(
    pcm: bytes,
    sample_rate: int,
    cfg: WhisperConfig,
) -> Transcript | None:
    """
    Run whisper.cpp on a PCM blob. Returns None on failure (so the caller can
    keep going — a bad transcription is not fatal to the daemon).
    """
    if not Path(cfg.binary).exists():
        log.error("whisper.cpp binary not found at %s", cfg.binary)
        return None
    if not Path(cfg.model).exists():
        log.error("whisper.cpp model not found at %s", cfg.model)
        return None

    cfg.work_dir.mkdir(parents=True, exist_ok=True)

    audio_seconds = len(pcm) / (sample_rate * 2)
    started = time.time()

    with tempfile.NamedTemporaryFile(
        prefix="scanrelay_",
        suffix=".wav",
        dir=str(cfg.work_dir),
        delete=False,
    ) as tmp:
        wav_path = Path(tmp.name)

    try:
        _write_wav(pcm, sample_rate, wav_path)

        cmd = [
            cfg.binary,
            "-m", cfg.model,
            "-f", str(wav_path),
            "-t", str(cfg.threads),
            "-bs", str(cfg.beam_size),
            "-l", "en",
            "-nt",            # no timestamps in output
            "-otxt",          # also write a .txt next to the wav
            "--no-prints",    # suppress whisper.cpp progress chatter
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=cfg.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("whisper.cpp timed out after %.1fs", cfg.timeout_seconds)
            return None

        if result.returncode != 0:
            log.warning(
                "whisper.cpp exited %d: %s",
                result.returncode,
                result.stderr.strip()[-300:],
            )
            return None

        # Prefer the .txt sidecar (cleaner) but fall back to stdout.
        txt_path = wav_path.with_suffix(".wav.txt")
        if txt_path.exists():
            text = txt_path.read_text(errors="replace").strip()
            try:
                txt_path.unlink()
            except OSError:
                pass
        else:
            text = result.stdout.strip()

        return Transcript(
            text=text,
            duration_audio_seconds=audio_seconds,
            duration_compute_seconds=time.time() - started,
        )

    finally:
        try:
            wav_path.unlink()
        except OSError:
            pass
