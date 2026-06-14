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
import math
import os
import signal
import struct
import subprocess
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
from .keyword_filter import Deduper, find_hit, get_priority_for_hit
from .mesh_sender import MeshSender
from .ntfy_sender import NtfySender
from .transcriber import transcribe, translate
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
        self.ntfy = NtfySender(cfg.ntfy)
        self.deduper = Deduper(cfg.filter.dedup_window_seconds)
        self.stop_flag = threading.Event()
        # Single worker so transcriptions don't pile up and starve the Pi.
        self.workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe")
        self.ntfy_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ntfy")
        self.event_log_path = cfg.log_dir / "events.jsonl"
        self.live_state_path = cfg.log_dir.parent / "live_state.json"
        self._last_level_write = 0.0
        self._last_hit_time = time.time()
        self._silence_alert_sent = False
        self._silence_thread: threading.Thread | None = None
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
        self._start_silence_watcher()

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
        self.ntfy_workers.shutdown(wait=True)
        self.sender.close()
        log.info("ScanRelay stopped")

    def _shutdown(self, *_args) -> None:
        log.info("Shutdown signal received")
        self.stop_flag.set()

    # --------------------------------------------------------------------- #
    # Core loop
    # --------------------------------------------------------------------- #

    def _run_once(self) -> None:
        frames = self._frames_with_audio_level(capture_frames(self.cfg.audio))
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

            # Optional translation: run a second whisper.cpp pass with --translate
            # when the detected language is non-English and translation is enabled.
            translation_cfg = self.cfg.translation
            translated = False
            if (
                translation_cfg.enabled
                and tr.language
                and tr.language not in translation_cfg.skip_languages
            ):
                log.info("Translating from %s", tr.language)
                translated_text = translate(
                    ev.pcm, ev.sample_rate, tr.language, self.cfg.whisper
                )
                if translated_text:
                    tr.text_original = tr.text
                    tr.text = translated_text
                    translated = True
                    log.info("Translated: %s", tr.text)

            # Keyword matching runs against the English (translated) text.
            # If no translation, tr.text is already the best we have.
            hit = find_hit(tr.text, self.cfg.filter)

            # Also try matching the original-language text in case the user
            # happened to add a keyword in the source language.
            if hit is None and tr.text_original:
                hit = find_hit(tr.text_original, self.cfg.filter)

            self._write_event_log(
                ev, tr.text, hit, event_id, audio_rel,
                language=tr.language,
                text_original=tr.text_original if translated else None,
                translated=translated,
            )

            if hit is None:
                return

            if not self.deduper.should_send(hit.excerpt):
                log.info("Deduped: %s", hit.excerpt[:80])
                return

            alert = format_alert(hit, ev.started_at, self.cfg.mesh)
            result = self.sender.send_text(alert.text)
            if not result.ok:
                log.error("Alert NOT delivered: %s", result.error)
                return

            self._last_hit_time = time.time()
            self._silence_alert_sent = False
            priority, tags = get_priority_for_hit(hit, self.cfg.filter)
            tags = tags or ["radio"]
            self.ntfy_workers.submit(
                self._send_ntfy_alert,
                hit.keyword,
                alert.text,
                priority,
                tags,
                audio_rel,
            )

        except Exception as e:
            log.exception("Event handler error: %s", e)

    def _send_ntfy_alert(
        self,
        keyword: str,
        alert_text: str,
        priority: int,
        tags: list[str],
        audio_rel: str | None,
    ) -> None:
        attach_path: Path | None = None
        if self.cfg.ntfy.attach_audio and audio_rel:
            wav_path = self.cfg.dashboard.audio_dir / audio_rel
            if wav_path.exists():
                try:
                    attach_path = self._convert_wav_to_mp3(wav_path)
                except Exception as e:
                    log.warning("ntfy audio attachment conversion failed: %s", e)
                    attach_path = None

        result = self.ntfy.send(
            title=f"ScanRelay: {keyword}",
            message=alert_text,
            priority=priority,
            tags=tags,
            attach_path=attach_path,
        )
        if not result.ok:
            log.warning("ntfy push failed: %s", result.error)

    def _convert_wav_to_mp3(self, wav_path: Path) -> Path:
        mp3_path = wav_path.with_suffix(".mp3")
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-t", str(max(1, int(self.cfg.ntfy.max_audio_seconds))),
            "-i", str(wav_path),
            "-ac", "1",
            "-b:a", "32k",
            str(mp3_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        return mp3_path

    def _start_silence_watcher(self) -> None:
        if not self.cfg.silence_alert.enabled or not self.cfg.ntfy.enabled:
            return
        self._silence_thread = threading.Thread(
            target=self._silence_watcher,
            name="silence-alert",
            daemon=True,
        )
        self._silence_thread.start()

    def _silence_watcher(self) -> None:
        threshold = max(1, int(self.cfg.silence_alert.threshold_seconds))
        check_every = min(300, max(30, threshold // 12))
        while not self.stop_flag.wait(check_every):
            if should_fire_silence_alert(
                last_hit_time=self._last_hit_time,
                now=time.time(),
                threshold_seconds=threshold,
                already_alerted=self._silence_alert_sent,
            ):
                hours = threshold / 3600.0
                result = self.ntfy.send(
                    title="ScanRelay silent",
                    message=f"No keyword hits in {hours:g} hours",
                    priority=4,
                    tags=["warning", "radio"],
                )
                if result.ok:
                    self._silence_alert_sent = True
                else:
                    log.warning("silence ntfy push failed: %s", result.error)

    def _frames_with_audio_level(self, frames):
        for frame in frames:
            now = time.time()
            if now - self._last_level_write >= 1.0:
                self._last_level_write = now
                self._write_live_state(audio_level_db=self._pcm_dbfs(frame))
            yield frame

    @staticmethod
    def _pcm_dbfs(pcm: bytes) -> float:
        if not pcm:
            return -120.0
        count = len(pcm) // 2
        if count == 0:
            return -120.0
        samples = struct.unpack("<" + "h" * count, pcm[: count * 2])
        rms = math.sqrt(sum(s * s for s in samples) / count)
        if rms <= 0:
            return -120.0
        return round(20.0 * math.log10(rms / 32768.0), 1)

    def _write_live_state(
        self,
        *,
        audio_level_db: float | None = None,
        event: dict | None = None,
        partial_transcript: str | None = None,
    ) -> None:
        try:
            self.live_state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {}
            if self.live_state_path.exists():
                try:
                    state = json.loads(self.live_state_path.read_text())
                except Exception:
                    state = {}
            state["updated_at"] = time.time()
            if audio_level_db is not None:
                state["audio_level"] = {"db": audio_level_db, "ts": time.time()}
            if partial_transcript is not None:
                state["partial_transcript"] = {
                    "text": partial_transcript,
                    "ts": time.time(),
                }
            if event is not None:
                state["last_event"] = event
            tmp = self.live_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self.live_state_path)
        except Exception as e:
            log.debug("Live state write skipped: %s", e)

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

    def _write_event_log(
        self,
        ev: VoiceEvent,
        text: str,
        hit,
        event_id: str,
        audio_file: str | None,
        *,
        language: str | None = None,
        text_original: str | None = None,
        translated: bool = False,
    ) -> None:
        """Append every transmission (hit or not) to a JSON line log for review."""
        try:
            self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
            record: dict = {
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
            }
            if language:
                record["language"] = language
            if translated and text_original is not None:
                record["text_original"] = text_original
                record["translated"] = True
            with open(self.event_log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            self._write_live_state(event=record)
        except Exception as e:
            log.warning("Event log write failed: %s", e)


def should_fire_silence_alert(
    *,
    last_hit_time: float,
    now: float,
    threshold_seconds: int,
    already_alerted: bool,
) -> bool:
    """Pure silence-alert decision helper for tests."""
    return (not already_alerted) and (now - last_hit_time) > threshold_seconds


def main() -> int:
    cfg = Config.load()
    setup_logging(cfg.log_dir)
    Daemon(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
