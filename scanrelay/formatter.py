"""
Format a keyword hit into the short text alert that goes over LoRa.

Target format (fits comfortably in one Meshtastic text packet):

    [SITE 22:53] <sentence(s) containing My Keyword from the transcript>

Whoever picks it up on a Heltec immediately knows:
  - it came from the ScanRelay node (site_tag)
  - what time the transmission happened
  - what the dispatcher actually said about My Keyword
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import MeshtasticConfig
from .keyword_filter import KeywordHit


@dataclass
class Alert:
    text: str
    timestamp: float


def format_alert(
    hit: KeywordHit,
    event_started_at: float,
    mesh_cfg: MeshtasticConfig,
) -> Alert:
    """
    Build the alert string. We use the *event* start time (when the dispatcher
    started talking), not "now", so the timestamp reflects when the incident
    was reported on the radio.
    """
    tz_name = getattr(mesh_cfg, "timezone", None)
    if tz_name:
        try:
            hhmm = datetime.fromtimestamp(event_started_at, ZoneInfo(tz_name)).strftime("%H:%M")
        except ZoneInfoNotFoundError:
            hhmm = time.strftime("%H:%M", time.localtime(event_started_at))
    else:
        hhmm = time.strftime("%H:%M", time.localtime(event_started_at))
    prefix = f"[{mesh_cfg.site_tag} {hhmm}] "

    # Compact whitespace; strip surrounding quotes/parens that whisper sometimes adds.
    body = hit.excerpt.strip().strip("\"'()[]")
    text = prefix + body
    return Alert(text=text, timestamp=event_started_at)
