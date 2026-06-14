"""Smoke tests for the ScanRelay v3.2.1 keyword filter."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanrelay.config import FilterConfig
from scanrelay.keyword_filter import Filter


def test_moss_lake_keyword_variants():
    f = Filter(FilterConfig())
    cases = [
        "respond to Moss Lake for a grass fire",
        "units near moss lk road",
        "mosslake marina report",
        "traffic near Moss-Lake bridge",
    ]
    for text in cases:
        assert f.find_hit(text) is not None


def test_non_matching_lake_text_ignored():
    f = Filter(FilterConfig())
    assert f.find_hit("respond to Cross Lake estates") is None


def test_1201_digit_and_spoken_variants():
    f = Filter(FilterConfig())
    cases = [
        "medical call at 1201 Main Street",
        "respond to twelve oh one Hickory",
        "address is twelve-oh-one Main Street",
        "at twelve hundred and one Elm",
        "at one thousand two hundred one Elm",
    ]
    for text in cases:
        assert f.find_hit(text) is not None


def test_1201_does_not_match_longer_numbers():
    f = Filter(FilterConfig())
    assert f.find_hit("incident at 12010 Main") is None
    assert f.find_hit("unit 31201 responding") is None
    assert f.find_hit("at twelve hundred two") is None
