"""
ScanRelay main daemon.

Pipeline:
    USB sound card  -> arecord  -> VAD  -> whisper.cpp  -> My Keyword filter
                                                                |
                                                                v
                                                       Meshtastic ch.2 alert

Run as: python -m scanrelay.daemon
Or via systemd: systemctl start scanrelay
"""
from __future__ import annotations

import json
import logging
import os
import signal
import struct
import sys
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .audio_capture import capture_frames, list_input_devices
from .config import Config
from .formatter import format_alert
from .keyword_filter import Deduper, find_hit
from .mesh_sender import MeshSender
from .transcriber import transcribe
from .vad_gate import gate, VoiceEvent

log = logging.getLogger("scanrelay")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "scanrelay.log"),
        ],
    )


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sender = MeshSender(cfg.mesh)
        self.deduper = Deduper(cfg.filter.dedup_window_seconds)
        self.stop_flag = threading.Event()
        # Single worker so transcriptions don't pile up and starve the Pi.
        self.workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe")
        self.event_log_path = cfg.log_dir / "events.jsonl"
        if cfg.dashboard.save_audio:
            cfg.dashboard.audio_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------------- #
    # Lifecycle
    # --------------------------------------------------------------------- #

    def run(self) -> None:
        log.info("ScanRelay starting")
        log.info("Audio device: %s", self.cfg.audio.device)
        log.info("Keywords: %s", self.cfg.filter.keywords)
        log.info("Meshtastic ch.%d @ %s:%d", self.cfg.mesh.channel_index,
                 self.cfg.mesh.host, self.cfg.mesh.port)

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

        backoff = 1.0
        while not self.stop_flag.is_set():
            try:
                self._run_once()
                backoff = 1.0
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error("Capture loop crashed: %s (restarting in %.1fs)", e, backoff)
                log.error("If this persists, check `arecord -l`:\n%s",
                          list_input_devices())
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 2, 30.0)

        self.workers.shutdown(wait=True)
        self.sender.close()
        log.info("ScanRelay stopped")

    def _shutdown(self, *_args) -> None:
        log.info("Shutdown signal received")
        self.stop_flag.set()

    # --------------------------------------------------------------------- #
    # Core loop
    # --------------------------------------------------------------------- #

    def _run_once(self) -> None:
        frames = capture_frames(self.cfg.audio)
        events = gate(frames, self.cfg.audio, self.cfg.vad)
        for ev in events:
            if self.stop_flag.is_set():
                break
            log.info("Transmission: %.2fs", ev.duration_seconds)
            # Offload transcription so the audio capture loop never blocks.
            self.workers.submit(self._handle_event, ev)

    def _handle_event(self, ev: VoiceEvent) -> None:
        try:
            event_id = f"{int(ev.started_at)}-{uuid.uuid4().hex[:8]}"
            audio_rel = self._save_event_audio(ev, event_id)

            tr = transcribe(ev.pcm, ev.sample_rate, self.cfg.whisper)
            if tr is None:
                log.warning("Transcription failed for %.2fs event", ev.duration_seconds)
                return
            log.info(
                "Transcript (%.2fs audio / %.2fs compute): %s",
                tr.duration_audio_seconds, tr.duration_compute_seconds, tr.text,
            )

            hit = find_hit(tr.text, self.cfg.filter)
            self._write_event_log(ev, tr.text, hit, event_id, audio_rel)

            if hit is None:
                return

            if not self.deduper.should_send(hit.excerpt):
                log.info("Deduped: %s", hit.excerpt[:80])
                return

            alert = format_alert(hit, ev.started_at, self.cfg.mesh)
            result = self.sender.send_text(alert.text)
            if not result.ok:
                log.error("Alert NOT delivered: %s", result.error)

        except Exception as e:
            log.exception("Event handler error: %s", e)

    def _save_event_audio(self, ev: VoiceEvent, event_id: str) -> str | None:
        """Persist the PCM buffer as a WAV so the dashboard can replay it."""
        if not self.cfg.dashboard.save_audio:
            return None
        try:
            fname = f"{event_id}.wav"
            path = self.cfg.dashboard.audio_dir / fname
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # S16_LE
                wf.setframerate(ev.sample_rate)
                wf.writeframes(ev.pcm)
            self._rotate_audio_dir()
            return fname
        except Exception as e:
            log.warning("Audio save failed: %s", e)
            return None

    def _rotate_audio_dir(self) -> None:
        """Keep audio dir under the configured file count + size cap."""
        try:
            d = self.cfg.dashboard.audio_dir
            files = sorted(d.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            # Trim by count
            while len(files) > self.cfg.dashboard.audio_max_files:
                files.pop(0).unlink(missing_ok=True)
            # Trim by size
            max_bytes = self.cfg.dashboard.audio_max_mb * 1024 * 1024
            total = sum(p.stat().st_size for p in files if p.exists())
            while total > max_bytes and files:
                victim = files.pop(0)
                try:
                    total -= victim.stat().st_size
                except FileNotFoundError:
                    pass
                victim.unlink(missing_ok=True)
        except Exception as e:
            log.debug("Audio rotation skipped: %s", e)

    def _write_event_log(self, ev: VoiceEvent, text: str, hit,
                         event_id: str, audio_file: str | None) -> None:
        """Append every transmission (hit or not) to a JSON line log for review."""
        try:
            self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.event_log_path, "a") as f:
                f.write(json.dumps({
                    "id": event_id,
                    "ts": ev.started_at,
                    "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.localtime(ev.started_at)),
                    "duration": round(ev.duration_seconds, 2),
                    "text": text,
                    "hit": bool(hit),
                    "keyword": hit.keyword if hit else None,
                    "excerpt": hit.excerpt if hit else None,
                    "audio_file": audio_file,
                }) + "\n")
        except Exception as e:
            log.warning("Event log write failed: %s", e)


def main() -> int:
    cfg = Config.load()
    setup_logging(cfg.log_dir)
    Daemon(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
