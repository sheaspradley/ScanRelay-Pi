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
import re
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from .config import WhisperConfig
from .tone_detector import detect_lead_tone

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    text: str
    duration_audio_seconds: float
    duration_compute_seconds: float
    # Language code detected by whisper (e.g. "en", "es"). None if unknown.
    language: str | None = None
    # Original (pre-translation) text. Populated only when translation ran.
    text_original: str | None = None
    # True if this transcript was machine-translated to English.
    translated: bool = False


def _write_wav(pcm: bytes, sample_rate: int, path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def _scrub_blacklist(text: str, blacklist: list[str]) -> str:
    """Remove standalone blacklisted phrases from *text*.

    A phrase is considered "standalone" when:
      - it matches the entire (stripped) transcript, OR
      - it is surrounded by whitespace, punctuation, or string boundaries.

    The rest of the transcript is always preserved.  Returns an empty string
    only when EVERY token in the transcript was a blacklisted phrase.

    The comparison is case-insensitive so "[Phone Ringing]" also matches.
    """
    if not text or not blacklist:
        return text

    # Build a set of lower-cased phrases for O(1) lookup.
    bl_lower = {p.lower() for p in blacklist}

    # Quick whole-transcript check (common fast path).
    if text.strip().lower() in bl_lower:
        return ""

    # Replace each blacklisted phrase with a sentinel, then clean up.
    # We escape the phrase for use in a regex and match it as a token.
    result = text
    for phrase in blacklist:
        # Build a pattern that matches the phrase surrounded by word/sentence
        # boundaries: start/end of string, whitespace, or common punctuation.
        escaped = re.escape(phrase)
        # Match the phrase when preceded/followed by non-word content or
        # start/end of string.  Use IGNORECASE so capitalisation variants match.
        pattern = r"(?:(?<=\s)|(?<=^)|(?<=[.!?,;:\"\'()\[\]]))" \
                  + escaped + \
                  r"(?=\s|$|[.!?,;:\"\'()\[\]])"
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)

    # Collapse multiple spaces / leading-trailing whitespace introduced by removals.
    result = re.sub(r"  +", " ", result).strip()
    return result


def transcribe(
    pcm: bytes,
    sample_rate: int,
    cfg: WhisperConfig,
) -> Transcript | None:
    """
    Run whisper.cpp on a PCM blob. Returns None on failure (so the caller can
    keep going — a bad transcription is not fatal to the daemon).

    Steps:
    1. (optional) Detect and skip leading dispatch tones.
    2. Write trimmed PCM to a temp WAV.
    3. Run whisper.cpp with the initial prompt and request JSON output for
       language detection.
    4. Scrub hallucinated blacklist phrases from the result.
    5. Return Transcript with text + detected language.
    """
    if not Path(cfg.binary).exists():
        log.error("whisper.cpp binary not found at %s", cfg.binary)
        return None
    if not Path(cfg.model).exists():
        log.error("whisper.cpp model not found at %s", cfg.model)
        return None

    cfg.work_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 1: lead-tone detection
    # ------------------------------------------------------------------
    working_pcm = pcm
    if cfg.tone_detect_enabled and pcm:
        tone_end, tone_conf = detect_lead_tone(
            pcm,
            sample_rate,
            window_seconds=cfg.tone_detect_window_seconds,
        )
        if tone_end > 0.0:
            skip_seconds = tone_end + cfg.tone_skip_seconds
            skip_bytes = int(skip_seconds * sample_rate * 2)
            # Never skip more than we have.
            skip_bytes = min(skip_bytes, len(pcm))
            working_pcm = pcm[skip_bytes:]
            log.info(
                "Skipped %.2fs of lead tones (conf=%.2f)",
                skip_seconds, tone_conf,
            )
            if len(working_pcm) < sample_rate * 2:
                # Less than 1 second of audio left — nothing to transcribe.
                log.info("After tone trim, clip too short; returning empty transcript")
                return Transcript(
                    text="",
                    duration_audio_seconds=len(pcm) / (sample_rate * 2),
                    duration_compute_seconds=0.0,
                    language=None,
                )

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
        _write_wav(working_pcm, sample_rate, wav_path)

        # ------------------------------------------------------------------
        # Step 2: build whisper.cpp command
        # ------------------------------------------------------------------
        cmd = [
            cfg.binary,
            "-m", cfg.model,
            "-f", str(wav_path),
            "-t", str(cfg.threads),
            "-bs", str(cfg.beam_size),
            "-nt",            # no timestamps in output
            "--no-prints",    # suppress whisper.cpp progress chatter
        ]

        # Initial prompt (hallucination suppression).
        if cfg.initial_prompt:
            cmd += ["--prompt", cfg.initial_prompt]

        # Request JSON output so we can extract detected language.
        # whisper.cpp writes <wav_path>.wav.json alongside the wav.
        cmd += ["-ojson"]

        # Also keep plain-text sidecar as fallback.
        cmd += ["-otxt"]

        # Language: use "auto" when translation may be needed so whisper
        # performs language detection.  If the model is *.en.* it ignores
        # this flag anyway.
        cmd += ["-l", "auto"]

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

        # ------------------------------------------------------------------
        # Step 3: read back text + language
        # ------------------------------------------------------------------
        detected_language: str | None = None
        text: str = ""

        # Try JSON sidecar first (gives us language info).
        json_path = wav_path.with_suffix(".wav.json")
        if json_path.exists():
            try:
                import json as _json
                jdata = _json.loads(json_path.read_text(errors="replace"))
                # whisper.cpp JSON schema: {"result":{"language":"en"}, "transcription":[...]}
                detected_language = (
                    jdata.get("result", {}).get("language")
                    or jdata.get("language")
                )
                # Collect all text segments.
                segments = jdata.get("transcription", [])
                if segments:
                    text = " ".join(
                        seg.get("text", "").strip()
                        for seg in segments
                        if seg.get("text", "").strip()
                    )
                else:
                    # Some builds use a flat "text" key.
                    text = jdata.get("text", "").strip()
            except Exception as exc:
                log.debug("JSON sidecar parse error: %s", exc)
            finally:
                try:
                    json_path.unlink()
                except OSError:
                    pass

        # Fall back to .txt sidecar.
        if not text:
            txt_path = wav_path.with_suffix(".wav.txt")
            if txt_path.exists():
                text = txt_path.read_text(errors="replace").strip()
                try:
                    txt_path.unlink()
                except OSError:
                    pass

        # Last resort: stdout.
        if not text:
            text = result.stdout.strip()

        # ------------------------------------------------------------------
        # Step 4: blacklist scrub
        # ------------------------------------------------------------------
        original_text = text
        if cfg.blacklist_phrases and text:
            text = _scrub_blacklist(text, cfg.blacklist_phrases)
            if text != original_text:
                log.debug(
                    "Blacklist scrub: %r -> %r",
                    original_text[:120], text[:120],
                )

        return Transcript(
            text=text,
            duration_audio_seconds=audio_seconds,
            duration_compute_seconds=time.time() - started,
            language=detected_language,
        )

    finally:
        try:
            wav_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Translation helper (second whisper.cpp pass with --translate)
# ---------------------------------------------------------------------------

def translate(
    pcm: bytes,
    sample_rate: int,
    source_lang: str,
    cfg: WhisperConfig,
) -> str | None:
    """Run a second whisper.cpp pass with --translate to produce English text.

    Uses the same PCM that was already transcribed.  Returns the translated
    English string, or None on failure.

    Note: whisper.cpp --translate only works with multilingual models
    (e.g. ggml-base.bin, NOT ggml-base.en.bin).  The caller should verify
    the model is multilingual before calling this function.
    """
    if not Path(cfg.binary).exists() or not Path(cfg.model).exists():
        return None

    cfg.work_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix="scanrelay_xlat_",
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
            "-l", source_lang,
            "--translate",    # force English output
            "-nt",
            "-otxt",
            "--no-prints",
        ]

        if cfg.initial_prompt:
            cmd += ["--prompt", cfg.initial_prompt]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=cfg.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("whisper.cpp translate timed out after %.1fs", cfg.timeout_seconds)
            return None

        if result.returncode != 0:
            log.warning(
                "whisper.cpp translate exited %d: %s",
                result.returncode,
                result.stderr.strip()[-300:],
            )
            return None

        txt_path = wav_path.with_suffix(".wav.txt")
        if txt_path.exists():
            text = txt_path.read_text(errors="replace").strip()
            try:
                txt_path.unlink()
            except OSError:
                pass
        else:
            text = result.stdout.strip()

        # Scrub blacklist from the translated text too.
        if cfg.blacklist_phrases and text:
            text = _scrub_blacklist(text, cfg.blacklist_phrases)

        return text or None

    finally:
        try:
            wav_path.unlink()
        except OSError:
            pass
