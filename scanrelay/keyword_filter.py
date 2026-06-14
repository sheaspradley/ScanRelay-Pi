"""
Keyword filter: does this transcript mention something we care about?

Two layers, both case-insensitive:
  1. Plain substring keywords (cheap, deterministic) - e.g. "my keyword"
  2. Regex patterns (for numbers with digit/word variants) - e.g. "12345" / "one two three four five"

A hit returns the matched needle and the sentence(s) surrounding it.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from .config import FilterConfig

log = logging.getLogger(__name__)


@dataclass
class KeywordHit:
    keyword: str          # the literal needle that matched (substring or regex source)
    matched_text: str     # the actual text span that matched (useful for regex)
    excerpt: str          # the sentence(s) around the hit
    full_text: str        # entire transcript


class Filter:
    """Compiled view of FilterConfig — built once, used many times."""

    def __init__(self, cfg: FilterConfig):
        self.keywords = [k.lower() for k in cfg.keywords]
        self.patterns = [
            re.compile(p, re.IGNORECASE) for p in cfg.keyword_patterns
        ]
        self.pattern_sources = list(cfg.keyword_patterns)

    def find_hit(self, text: str) -> KeywordHit | None:
        """Return the first hit (substring or regex) or None."""
        if not text:
            return None

        lower = text.lower()

        # Layer 1: substring keywords.
        for kw in self.keywords:
            if kw in lower:
                excerpt = _extract_sentences_containing(text, kw)
                return KeywordHit(
                    keyword=kw,
                    matched_text=kw,
                    excerpt=excerpt,
                    full_text=text,
                )

        # Layer 2: regex patterns.
        for src, pat in zip(self.pattern_sources, self.patterns):
            m = pat.search(text)
            if m:
                excerpt = _extract_sentences_containing(text, m.group(0).lower())
                return KeywordHit(
                    keyword=src,
                    matched_text=m.group(0),
                    excerpt=excerpt,
                    full_text=text,
                )

        return None


def _extract_sentences_containing(text: str, needle_lower: str) -> str:
    """
    Pull out the sentence(s) containing the needle. Splits on sentence-ish
    boundaries (., !, ?). Falls back to full text if splitting yields nothing.
    """
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    hits = [p.strip() for p in parts if needle_lower in p.lower()]
    if not hits:
        return text.strip()
    return re.sub(r"\s+", " ", " ".join(hits)).strip()


# Backwards-compatible function form (used by daemon.py).
def find_hit(text: str, cfg: FilterConfig) -> KeywordHit | None:
    return Filter(cfg).find_hit(text)


class Deduper:
    """
    Suppress duplicate alerts within a rolling window.

    Dispatchers repeat themselves and the scanner re-broadcasts. We don't want
    the mesh to see the same alert five times in 30 seconds.
    """

    def __init__(self, window_seconds: float):
        self.window = window_seconds
        self._recent: dict[str, float] = {}

    def should_send(self, excerpt: str) -> bool:
        now = time.time()
        for k, ts in list(self._recent.items()):
            if now - ts > self.window:
                del self._recent[k]
        key = excerpt.lower().strip()
        if key in self._recent:
            log.debug("Dedup suppressed: %s", key[:60])
            return False
        self._recent[key] = now
        return True
