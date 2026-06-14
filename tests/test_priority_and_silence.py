from __future__ import annotations

from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.modules.setdefault("webrtcvad", types.SimpleNamespace(Vad=lambda *_args, **_kwargs: None))

from scanrelay.config import FilterConfig, KeywordPriority, Config
from scanrelay.keyword_filter import KeywordHit, get_priority_for_hit
from scanrelay.daemon import should_fire_silence_alert


def test_get_priority_for_exact_keyword():
    cfg = FilterConfig(keyword_priorities=[
        KeywordPriority(keyword="moss lake", priority=5, tags=["siren", "fire"])
    ])
    hit = KeywordHit(keyword="moss lake", matched_text="moss lake", excerpt="x", full_text="x")
    assert get_priority_for_hit(hit, cfg) == (5, ["siren", "fire"])


def test_get_priority_default_for_no_match():
    cfg = FilterConfig(keyword_priorities=[])
    hit = KeywordHit(keyword="moss lk", matched_text="moss lk", excerpt="x", full_text="x")
    assert get_priority_for_hit(hit, cfg) == (3, [])


def test_config_loads_keyword_priorities(tmp_path):
    path = tmp_path / "scanrelay.toml"
    path.write_text('''\n[filter]\nkeywords = ["moss lake"]\n[[filter.keyword_priorities]]\nkeyword = "moss lake"\npriority = 5\ntags = ["siren"]\n''')
    cfg = Config.load(path)
    assert cfg.filter.keyword_priorities[0].keyword == "moss lake"
    assert cfg.filter.keyword_priorities[0].priority == 5
    assert cfg.filter.keyword_priorities[0].tags == ["siren"]


def test_silence_alert_fires_once_after_threshold():
    assert should_fire_silence_alert(
        last_hit_time=100.0,
        now=100.0 + 14401,
        threshold_seconds=14400,
        already_alerted=False,
    ) is True
    assert should_fire_silence_alert(
        last_hit_time=100.0,
        now=100.0 + 14401,
        threshold_seconds=14400,
        already_alerted=True,
    ) is False


def test_silence_alert_does_not_fire_before_threshold():
    assert should_fire_silence_alert(
        last_hit_time=100.0,
        now=100.0 + 1000,
        threshold_seconds=14400,
        already_alerted=False,
    ) is False
