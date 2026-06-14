"""
ScanRelay configuration — My Keyword keyword filter mode.

Defaults are hard-coded here. Override at runtime by editing
/etc/scanrelay/scanrelay.toml — see scanrelay.toml.example.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SCANRELAY_CONFIG", "/etc/scanrelay/scanrelay.toml"))


@dataclass
class AudioConfig:
    # ALSA device name. Find with: arecord -l
    # USB sound card typically shows as "plughw:CARD=Device,DEV=0".
    device: str = "plughw:CARD=Device,DEV=0"
    sample_rate: int = 16000          # whisper.cpp wants 16k
    channels: int = 1
    frame_ms: int = 30                # webrtcvad supports 10, 20, 30
    preroll_seconds: float = 0.5      # captured before VAD trigger so we don't clip start


@dataclass
class VADConfig:
    aggressiveness: int = 2           # 0 (loose) - 3 (strict)
    start_frames: int = 6             # ~180ms of voice to call it a transmission
    end_frames: int = 25              # ~750ms of silence to call it over
    min_event_seconds: float = 1.0    # ignore squelch tails / mic key-ups
    max_event_seconds: float = 30.0   # cap on a single transmission


@dataclass
class WhisperConfig:
    # Path to whisper.cpp `main` binary (build from github.com/ggerganov/whisper.cpp).
    binary: str = "/opt/whisper.cpp/main"
    # Path to quantized model. base.en-q5_1 is the sweet spot on Pi 5.
    model: str = "/opt/whisper.cpp/models/ggml-base.en-q5_1.bin"
    # CPU threads for inference. Pi 5 has 4 cores; leave one for the OS.
    threads: int = 3
    # Beam search size. 1 = greedy (fast). 5 = more accurate but slower.
    beam_size: int = 1
    # Hard timeout (seconds) on a single transcription before we give up.
    timeout_seconds: float = 30.0
    # --- Tone / hallucination suppression ---
    # Enable lead-tone detection (strips dispatch tones before transcription).
    tone_detect_enabled: bool = True
    # Analyse this many seconds at the start of each clip.
    tone_detect_window_seconds: float = 8.0
    # RMS threshold (dBFS) — tones are typically loud and narrow-band.
    tone_detect_threshold_db: float = -25.0
    # Safety pad appended past the detected tone end before we start transcribing.
    tone_skip_seconds: float = 0.5
    # Seed prompt so Whisper understands the audio domain.
    initial_prompt: str = (
        "Police, fire, and EMS scanner radio dispatch. "
        "Ignore tones, beeps, pages, alert signals, and silence."
    )
    # Exact phrases that, when they appear ALONE or dominate the output, get scrubbed.
    blacklist_phrases: list[str] = field(default_factory=lambda: [
        "[phone ringing]", "[ringing]", "ring ring",
        "[music]", "[music playing]", "[applause]", "[laughter]", "[silence]",
        "thank you.", "thanks for watching.", "thanks for watching!",
        "please subscribe.", "subscribe to my channel.",
        "don't forget to subscribe.", "like and subscribe.",
        "\u266a", "\u266b", "(beep)", "[beep]", "beep beep", "[bell]",
        "[alarm]", "(alarm)",
    ])
    # Where to drop the temporary WAV files we feed whisper.
    work_dir: Path = Path("/var/lib/scanrelay/tmp")


@dataclass
class KeywordPriority:
    # Exact match against the KeywordHit.keyword string.
    keyword: str
    # ntfy priority: 1=min, 2=low, 3=default, 4=high, 5=urgent.
    priority: int = 5
    # ntfy tags such as ["siren", "fire"].
    tags: list[str] = field(default_factory=list)


@dataclass
class FilterConfig:
    # Case-insensitive substring keywords. Any hit triggers a relay.
    # Variants matter — Whisper may drop spaces or use abbreviations.
    keywords: list[str] = field(default_factory=lambda: [
        "moss lake",
        "moss lk",
        "mosslake",
        "moss-lake",
    ])
    # Regex patterns (case-insensitive). Use these where Whisper may emit
    # digits OR words for the same value. Match exactly "12345" (e.g. CR 12345,
    # FM 12345, or 12345 as a standalone house/route number).
    keyword_patterns: list[str] = field(default_factory=lambda: [
        # Digits form. \b ensures we don't match inside longer numbers.
        r"\b1201\b",
        # Whisper word forms for 1201:
        #   "twelve-oh-one"
        r"\btwelve[\s-]+oh[\s-]+one\b",
        r"\btwelve[\s-]+hundred(?:[\s-]+and)?[\s-]+one\b",
        r"\bone[\s-]+thousand[\s-]+two[\s-]+hundred(?:[\s-]+and)?[\s-]+one\b",
    ])
    # Suppress duplicate alerts containing the same excerpt within this window.
    dedup_window_seconds: float = 30.0
    # Per-keyword ntfy priority/tag overrides.
    keyword_priorities: list[KeywordPriority] = field(default_factory=list)


@dataclass
class MeshtasticConfig:
    host: str = "127.0.0.1"
    port: int = 4403
    channel_index: int = 1            # Scanner private channel
    # Meshtastic text payload caps near 228 bytes; we stay well under.
    max_text_bytes: int = 200
    # Minimum seconds between transmits — protect the airwaves.
    min_send_interval_seconds: float = 4.0
    # Site/location tag prepended to every alert.
    site_tag: str = "SITE"
    # IANA timezone used for the HH:MM stamp in alerts. Set to None to use
    # the system local time (which may be UTC under systemd).
    timezone: str | None = "America/Chicago"


@dataclass
class NtfyConfig:
    enabled: bool = False
    topic: str = ""
    server: str = "https://ntfy.sh"
    attach_audio: bool = True
    max_audio_seconds: int = 60


@dataclass
class DashboardConfig:
    # Save WAVs of every transmission so the dashboard can replay them.
    # ~32 KB/sec at 16 kHz mono — keep rotation tight.
    save_audio: bool = True
    audio_dir: Path = Path("/var/lib/scanrelay/audio")
    # Rotation policy: keep at most this many WAVs OR this many MB, whichever is smaller.
    audio_max_files: int = 500
    audio_max_mb: int = 250


@dataclass
class TranslationConfig:
    """Optional on-device translation via whisper.cpp --translate."""
    # Set enabled = true to translate non-English transmissions to English.
    enabled: bool = False
    # Languages that do NOT need translation (skip the second pass).
    skip_languages: list[str] = field(default_factory=lambda: ["en"])
    # Minimum whisper language-detection confidence to attempt translation.
    min_confidence: float = 0.65


@dataclass
class SilenceAlertConfig:
    """Push an ntfy alert when no keyword hits have arrived for too long."""
    enabled: bool = True
    threshold_seconds: int = 14400


@dataclass
class AISummaryConfig:
    """Optional OpenAI-powered daily narrative summary."""
    enabled: bool = False
    # AI provider. Only "openai" is supported currently.
    provider: str = "openai"
    # Chat model to use. gpt-4o-mini is cheap and good enough for summaries.
    model: str = "gpt-4o-mini"
    # Literal API key (leave empty to use api_key_env instead).
    api_key: str = ""
    # Environment variable to read the key from when api_key is empty.
    api_key_env: str = "OPENAI_API_KEY"
    # Cap on transcript characters sent to the API (cost control).
    max_input_chars: int = 12000
    # System-level style instruction for the summary.
    style: str = (
        "Write a single tight paragraph (3-5 sentences) summarizing what happened on "
        "the scanner today for a non-technical reader. Mention notable incidents, "
        "locations if named, and the overall tempo of the day. Skip routine traffic "
        "stops unless they dominate. Do not invent details. "
        "Do not use bullet points or headings."
    )


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    mesh: MeshtasticConfig = field(default_factory=MeshtasticConfig)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    ai_summary: AISummaryConfig = field(default_factory=AISummaryConfig)
    silence_alert: SilenceAlertConfig = field(default_factory=SilenceAlertConfig)
    log_dir: Path = Path("/var/lib/scanrelay/logs")

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        cfg = cls()
        if not path.exists():
            return cfg
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # `from __future__ import annotations` makes f.type a string, so we
        # compare against the string "Path" to detect Path-typed fields.
        def _coerce(field_type: str, v):
            if field_type == "Path" and isinstance(v, str):
                return Path(v)
            return v

        for section, values in data.items():
            if hasattr(cfg, section) and isinstance(values, dict):
                sub = getattr(cfg, section)
                sub_types = {f.name: f.type for f in fields(sub)}
                for k, v in values.items():
                    if hasattr(sub, k):
                        if section == "filter" and k == "keyword_priorities":
                            setattr(sub, k, [
                                KeywordPriority(
                                    keyword=str(item.get("keyword", "")),
                                    priority=int(item.get("priority", 5)),
                                    tags=list(item.get("tags", [])),
                                )
                                for item in v
                                if isinstance(item, dict)
                            ])
                        else:
                            setattr(sub, k, _coerce(sub_types.get(k, ""), v))
            elif hasattr(cfg, section):
                # Top-level field (e.g. log_dir).
                top_types = {f.name: f.type for f in fields(cfg)}
                setattr(cfg, section, _coerce(top_types.get(section, ""), values))
        return cfg

    def dump(self) -> dict:
        return asdict(self)
