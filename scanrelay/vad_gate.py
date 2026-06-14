"""
Voice Activity Detection gate.

Wraps webrtcvad with start/end hysteresis to emit complete transmission events.
This version KEEPS the audio so we can transcribe it.
"""
from __future__ import annotations

import collections
import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import webrtcvad

from .config import AudioConfig, VADConfig

log = logging.getLogger(__name__)


@dataclass
class VoiceEvent:
    """A complete detected transmission with audio attached."""
    pcm: bytes                 # raw 16-bit PCM at audio.sample_rate
    sample_rate: int
    started_at: float          # unix timestamp
    duration_seconds: float


def gate(
    frames: Iterable[bytes],
    audio: AudioConfig,
    vad_cfg: VADConfig,
) -> Iterator[VoiceEvent]:
    """
    Consume PCM frames and yield a VoiceEvent for each utterance.

    Frames must be exactly `frame_bytes(audio)` long and match `audio.sample_rate`.
    """
    vad = webrtcvad.Vad(vad_cfg.aggressiveness)

    preroll_frames = max(1, int(audio.preroll_seconds * 1000 / audio.frame_ms))
    ring: collections.deque[bytes] = collections.deque(maxlen=preroll_frames)

    triggered = False
    voiced_run = 0
    silence_run = 0
    event_buf: list[bytes] = []
    event_start: float = 0.0

    max_event_frames = int(vad_cfg.max_event_seconds * 1000 / audio.frame_ms)
    min_event_frames = int(vad_cfg.min_event_seconds * 1000 / audio.frame_ms)

    for frame in frames:
        try:
            is_speech = vad.is_speech(frame, audio.sample_rate)
        except Exception as e:
            log.warning("VAD error (frame size %d): %s", len(frame), e)
            continue

        if not triggered:
            ring.append(frame)
            if is_speech:
                voiced_run += 1
                if voiced_run >= vad_cfg.start_frames:
                    triggered = True
                    event_start = time.time()
                    event_buf = list(ring)        # prepend pre-roll
                    ring.clear()
                    voiced_run = 0
                    silence_run = 0
                    log.debug("VAD: transmission start")
            else:
                voiced_run = 0
        else:
            event_buf.append(frame)
            if is_speech:
                silence_run = 0
            else:
                silence_run += 1

            too_long = len(event_buf) >= max_event_frames
            ended = silence_run >= vad_cfg.end_frames

            if ended or too_long:
                duration = len(event_buf) * audio.frame_ms / 1000.0
                if len(event_buf) >= min_event_frames:
                    log.debug(
                        "VAD: transmission end (%.2fs, forced=%s)",
                        duration, too_long,
                    )
                    yield VoiceEvent(
                        pcm=b"".join(event_buf),
                        sample_rate=audio.sample_rate,
                        started_at=event_start,
                        duration_seconds=duration,
                    )
                else:
                    log.debug("VAD: too short, discarded (%.2fs)", duration)
                triggered = False
                event_buf = []
                silence_run = 0
                voiced_run = 0
