"""
scanrelay/categorize.py — heuristic rule-based event categorizer.

Used by the dashboard server to label events with a category for display.
Does NOT modify the daemon or its output — applied on-the-fly in API responses.

Categories (in priority order):
  FIRE     — fire-related traffic
  MEDICAL  — EMS / medical traffic
  TRAFFIC  — traffic enforcement / crashes
  WEATHER  — weather alerts
  DISPATCH — general dispatch / comms
  UNKNOWN  — nothing matched
"""

from __future__ import annotations
import re
from functools import lru_cache

# ---------------------------------------------------------------------------
# Rule definitions  (category → list of regex patterns, case-insensitive)
# ---------------------------------------------------------------------------
_RULES: list[tuple[str, list[str]]] = [
    ("FIRE", [
        r"\bengine\s*\d+",
        r"\bbrush\s+fire\b",
        r"\bstructure\s+fire\b",
        r"\bsmoke\b",
        r"\bfire\b",
        r"\bflames?\b",
        r"\bburn(ing)?\b",
        r"\barson\b",
        r"\bgas\s+leak\b",
        r"\bhazmat\b",
    ]),
    ("MEDICAL", [
        r"\bems\b",
        r"\bambulance\b",
        r"\bcode\s+3\b",
        r"\bmedical\b",
        r"\bcardiac\b",
        r"\bpulse\b",
        r"\btrauma\b",
        r"\bparamedic\b",
        r"\binjur(y|ed|ies)\b",
        r"\boverdo(se|sed)\b",
        r"\bunconscious\b",
        r"\brescue\b",
        r"\bmedic\s*\d+",
    ]),
    ("TRAFFIC", [
        r"\btraffic\s+stop\b",
        r"\baccident\b",
        r"\bmile\s+marker\b",
        r"\bhighway\b",
        r"\bspeeding\b",
        r"\bcrash\b",
        r"\bcollision\b",
        r"\bvehi(cle|cular)\b",
        r"\bpursuit\b",
        r"\bdriving\b",
        r"\bDUI\b",
        r"\btow\b",
        r"\bnorth\s*bound\b",
        r"\bsouth\s*bound\b",
        r"\beast\s*bound\b",
        r"\bwest\s*bound\b",
    ]),
    ("WEATHER", [
        r"\bstorm\b",
        r"\btornado\b",
        r"\bhail\b",
        r"\bwind(s|y)?\b",
        r"\bwarning\b",
        r"\bwatch\b",
        r"\bflood(ing)?\b",
        r"\blightn(ing|ings?)\b",
        r"\beverely?\b",
        r"\bweather\b",
        r"\bthunder\b",
    ]),
    ("DISPATCH", [
        r"\bdispatch\b",
        r"\bcopy\b",
        r"\b10-4\b",
        r"\bunits?\b",
        r"\bclear\b",
        r"\bresponding\b",
        r"\bstandby\b",
        r"\ben\s+route\b",
        r"\bon\s+scene\b",
        r"\bsignal\s+\d+",
        r"\bbase\s+to\b",
        r"\b(north|south|east|west)\s+unit\b",
    ]),
]

# Pre-compile all patterns once
_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (cat, [re.compile(p, re.IGNORECASE) for p in pats])
    for cat, pats in _RULES
]


def categorize(text: str) -> str:
    """Return the best-matching category label for the given transcript text."""
    if not text:
        return "UNKNOWN"
    for cat, patterns in _COMPILED:
        for pat in patterns:
            if pat.search(text):
                return cat
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Per-event caching (keyed by event id so we never recompute)
# ---------------------------------------------------------------------------
_cache: dict[str, str] = {}


def categorize_event(ev: dict) -> str:
    """Cached wrapper — pass an event dict, get category string back."""
    eid = ev.get("id") or f"{ev.get('ts',0)}-{(ev.get('text') or '')[:20]}"
    if eid not in _cache:
        _cache[eid] = categorize(ev.get("text") or "")
    return _cache[eid]


def clear_cache() -> None:
    _cache.clear()
