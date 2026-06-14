"""
Tests for the _scrub_blacklist helper in scanrelay.transcriber.

The spec says the helper may be private (underscore prefix) but must be
importable for testing. We expose it at module level as ``scrub_blacklist``
(no underscore) and also keep the private alias so existing code is unaffected.

Run with: python -m pytest tests/test_blacklist.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make sure the package is importable from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the helper. It lives as _scrub_blacklist; we access it directly.
from scanrelay.transcriber import _scrub_blacklist as scrub_blacklist


# Default blacklist from WhisperConfig — use a representative subset.
DEFAULT_BLACKLIST = [
    "[phone ringing]", "[ringing]", "ring ring",
    "[music]", "[music playing]", "[applause]", "[laughter]", "[silence]",
    "thank you.", "thanks for watching.", "thanks for watching!",
    "please subscribe.", "subscribe to my channel.",
    "don't forget to subscribe.", "like and subscribe.",
    "\u266a", "\u266b", "(beep)", "[beep]", "beep beep", "[bell]",
    "[alarm]", "(alarm)",
]


# ---------------------------------------------------------------------------
# Case 1: lone blacklisted phrase → empty string
# ---------------------------------------------------------------------------

def test_lone_phone_ringing():
    """'[phone ringing]' alone must be fully removed."""
    result = scrub_blacklist("[phone ringing]", DEFAULT_BLACKLIST)
    assert result == "", f"Expected empty string, got {result!r}"


# ---------------------------------------------------------------------------
# Case 2: blacklisted phrase prepended to real text → phrase removed, text kept
# ---------------------------------------------------------------------------

def test_prefix_removed_content_kept():
    """'[phone ringing] vehicle at mile 42' → 'vehicle at mile 42'."""
    result = scrub_blacklist("[phone ringing] vehicle at mile 42", DEFAULT_BLACKLIST)
    assert "[phone ringing]" not in result.lower(), (
        f"Phrase not removed from: {result!r}"
    )
    assert "vehicle at mile 42" in result, (
        f"Real content was incorrectly removed: {result!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: real dispatch text → completely unchanged
# ---------------------------------------------------------------------------

def test_dispatch_text_unchanged():
    """Real scanner text must pass through without modification."""
    text = "the dispatcher said unit 304 respond"
    result = scrub_blacklist(text, DEFAULT_BLACKLIST)
    assert result == text, f"Text was unexpectedly modified: {result!r}"


# ---------------------------------------------------------------------------
# Case 4: musical note symbol → empty string
# ---------------------------------------------------------------------------

def test_music_note_symbol():
    """The '♪' music note symbol must be scrubbed."""
    result = scrub_blacklist("\u266a", DEFAULT_BLACKLIST)
    assert result == "", f"Expected empty string after scrubbing ♪, got {result!r}"


# ---------------------------------------------------------------------------
# Case 5: 'thank you.' alone → empty string
# ---------------------------------------------------------------------------

def test_thank_you_alone():
    """'thank you.' as the entire transcript must be removed."""
    result = scrub_blacklist("thank you.", DEFAULT_BLACKLIST)
    assert result == "", f"Expected empty string for 'thank you.', got {result!r}"


# ---------------------------------------------------------------------------
# Case 6: 'thank you' embedded in real text → unchanged
# ---------------------------------------------------------------------------

def test_thank_you_embedded():
    """'thank you for the information about the fire' must NOT be modified.

    Only the exact phrase 'thank you.' (with trailing dot) is blacklisted,
    and only when it appears as a standalone token — not as a substring of
    a longer meaningful phrase.
    """
    text = "thank you for the information about the fire"
    result = scrub_blacklist(text, DEFAULT_BLACKLIST)
    assert result == text, (
        f"Real text was incorrectly scrubbed: {result!r}"
    )


# ---------------------------------------------------------------------------
# Case 7: empty input → empty output (no crash)
# ---------------------------------------------------------------------------

def test_empty_string():
    """Empty input returns empty output without error."""
    result = scrub_blacklist("", DEFAULT_BLACKLIST)
    assert result == ""


# ---------------------------------------------------------------------------
# Case 8: blacklist with only phrase → after scrub collapses to ""
# ---------------------------------------------------------------------------

def test_multiple_blacklisted_tokens():
    """Multiple blacklisted tokens on one line → fully scrubbed."""
    result = scrub_blacklist("[music] [applause]", DEFAULT_BLACKLIST)
    assert result.strip() == "", (
        f"Expected fully scrubbed, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Case 9: case-insensitivity
# ---------------------------------------------------------------------------

def test_case_insensitive():
    """Blacklist matching must be case-insensitive."""
    result = scrub_blacklist("[Phone Ringing]", DEFAULT_BLACKLIST)
    assert result == "", f"Expected empty (case-insensitive), got {result!r}"


# ---------------------------------------------------------------------------
# Case 10: (beep) embedded mid-sentence, real content kept
# ---------------------------------------------------------------------------

def test_beep_mid_sentence():
    """'(beep)' removed from middle of sentence; surrounding text kept."""
    text = "Engine 7 (beep) respond to Main Street"
    result = scrub_blacklist(text, DEFAULT_BLACKLIST)
    assert "(beep)" not in result.lower(), f"(beep) was not removed from: {result!r}"
    assert "Engine 7" in result, f"Leading content lost: {result!r}"
    assert "respond to Main Street" in result, f"Trailing content lost: {result!r}"
