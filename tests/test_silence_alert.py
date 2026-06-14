"""
Tests for silence-alerting logic (ScanRelay v3.2.1).

The _silence_watcher is an async background task that we can't trivially unit
test end-to-end without a running event loop and real files. Instead we test
the *decision logic* by extracting the key predicate into a helper.

We also test the SilenceAlertConfig dataclass defaults and TOML loading.
"""
from __future__ import annotations

import json
import os
import sys
import time
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import helpers we can test without spinning up FastAPI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CST = timezone(timedelta(hours=-6))  # America/Chicago (CST, winter)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 15, hour, minute, 0, tzinfo=_CST)


def _make_events_file(events: list[dict]) -> Path:
    """Write a minimal events.jsonl to a temp file and return its path."""
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for ev in events:
        tf.write(json.dumps(ev) + "\n")
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# Pure silence-detection predicate (extracted for testing)
# ---------------------------------------------------------------------------

def silence_should_alert(
    last_event_ts: float | None,
    now_ts: float,
    threshold_hours: float,
    silence_active: bool,
) -> tuple[bool, bool]:
    """Return (should_fire_alert, should_fire_recovery).

    Mirrors the logic in _silence_watcher without async/IO.
    """
    age_hours = (now_ts - last_event_ts) / 3600.0 if last_event_ts else None

    fire_alert    = (age_hours is not None and age_hours >= threshold_hours
                     and not silence_active)
    fire_recovery = ((age_hours is None or age_hours < threshold_hours)
                     and silence_active)
    return fire_alert, fire_recovery


# ---------------------------------------------------------------------------
# Tests: silence detection predicate
# ---------------------------------------------------------------------------

class TestSilenceDetectionLogic:

    def _now(self):
        return time.time()

    def test_no_last_event_never_fires(self):
        """If events.jsonl is empty/missing, age is None — don't alert."""
        fire, recover = silence_should_alert(None, time.time(), 4.0, False)
        assert not fire
        assert not recover

    def test_recent_event_no_alert(self):
        now = time.time()
        last = now - 1 * 3600  # 1 hour ago — under 4h threshold
        fire, recover = silence_should_alert(last, now, 4.0, False)
        assert not fire

    def test_old_event_triggers_alert(self):
        now = time.time()
        last = now - 5 * 3600  # 5 hours ago — over 4h threshold
        fire, recover = silence_should_alert(last, now, 4.0, False)
        assert fire

    def test_alert_not_fired_twice(self):
        """With silence_active=True already, alert must not fire again."""
        now = time.time()
        last = now - 6 * 3600
        fire, recover = silence_should_alert(last, now, 4.0, True)
        assert not fire

    def test_recovery_when_active_and_new_event(self):
        now = time.time()
        last = now - 0.5 * 3600  # recent event — scanner is back
        _, recover = silence_should_alert(last, now, 4.0, True)
        assert recover

    def test_no_recovery_when_not_active(self):
        now = time.time()
        last = now - 0.5 * 3600
        _, recover = silence_should_alert(last, now, 4.0, False)
        assert not recover

    def test_threshold_boundary_exact(self):
        """At exactly threshold_hours the alert fires (>=)."""
        now = time.time()
        last = now - 4.0 * 3600
        fire, _ = silence_should_alert(last, now, 4.0, False)
        assert fire

    def test_threshold_just_under(self):
        """Just under threshold: no alert."""
        now = time.time()
        last = now - (4.0 * 3600 - 60)  # 1 minute under
        fire, _ = silence_should_alert(last, now, 4.0, False)
        assert not fire

    def test_custom_threshold(self):
        """Custom threshold of 1 hour."""
        now = time.time()
        last = now - 1.5 * 3600  # 90 minutes — over 1h threshold
        fire, _ = silence_should_alert(last, now, 1.0, False)
        assert fire

    def test_alert_decision_has_no_time_window_suppression(self):
        """The daemon-side predicate only checks age and one-shot state."""
        now = time.time()
        last = now - 5 * 3600
        fire, _ = silence_should_alert(last, now, 4.0, False)
        assert fire


# ---------------------------------------------------------------------------
# Tests: SilenceAlertConfig dataclass
# ---------------------------------------------------------------------------

class TestSilenceAlertConfig:

    def test_defaults(self):
        from scanrelay.config import SilenceAlertConfig
        c = SilenceAlertConfig()
        assert c.enabled is True
        assert c.threshold_seconds == 14400

    def test_in_config(self):
        """SilenceAlertConfig is wired into Config."""
        from scanrelay.config import Config
        cfg = Config()
        assert hasattr(cfg, "silence_alert")
        assert cfg.silence_alert.threshold_seconds == 14400

    def test_toml_load(self):
        """Config.load() correctly reads silence_alert section from TOML."""
        import tempfile, tomllib
        from scanrelay.config import Config
        toml_text = b"""
[silence_alert]
enabled = true
threshold_seconds = 7200
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as tf:
            tf.write(toml_text)
            tf.flush()
            cfg = Config.load(Path(tf.name))
        os.unlink(tf.name)

        assert cfg.silence_alert.enabled is True
        assert cfg.silence_alert.threshold_seconds == 7200


# ---------------------------------------------------------------------------
# Tests: reading last event timestamp from events.jsonl
# ---------------------------------------------------------------------------

class TestLastEventTimestamp:
    """Validate the tail-read logic used by _silence_watcher."""

    def _read_last_ts(self, path: Path) -> float | None:
        """Replicate the tail-read from _silence_watcher."""
        last_ts = None
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                sz = f.tell()
                f.seek(max(0, sz - 4096))
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    ts = ev.get("ts")
                    if ts:
                        last_ts = float(ts)
                        break
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        return last_ts

    def test_empty_file_returns_none(self):
        p = _make_events_file([])
        try:
            assert self._read_last_ts(p) is None
        finally:
            os.unlink(p)

    def test_single_event(self):
        ts = 1700000000.0
        p = _make_events_file([{"id": "x", "ts": ts, "hit": False}])
        try:
            assert self._read_last_ts(p) == ts
        finally:
            os.unlink(p)

    def test_multiple_events_returns_last(self):
        events = [
            {"id": "a", "ts": 1700000000.0, "hit": False},
            {"id": "b", "ts": 1700001000.0, "hit": False},
            {"id": "c", "ts": 1700002000.0, "hit": True},
        ]
        p = _make_events_file(events)
        try:
            # Last line has ts=1700002000.0
            assert self._read_last_ts(p) == 1700002000.0
        finally:
            os.unlink(p)

    def test_missing_file_returns_none(self):
        assert self._read_last_ts(Path("/nonexistent/path/events.jsonl")) is None
