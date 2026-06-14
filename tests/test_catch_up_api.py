"""
Tests for /api/catch-up and /api/last-seen endpoints (ScanRelay v3.2.1).

Uses FastAPI TestClient so we exercise the actual HTTP layer without a running
server. events.jsonl is injected via the SCANRELAY_EVENTS environment variable
so no real filesystem paths are needed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from fastapi.testclient import TestClient
    _has_testclient = True
except ImportError:
    _has_testclient = False

pytestmark = pytest.mark.skipif(
    not _has_testclient,
    reason="fastapi[testclient] / httpx not installed"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_events_path(tmp_path, monkeypatch):
    """Point SCANRELAY_EVENTS at a fresh temp file for each test."""
    events_file = tmp_path / "events.jsonl"
    events_file.touch()
    monkeypatch.setenv("SCANRELAY_EVENTS", str(events_file))
    # Also patch the module-level constant that was already imported
    import dashboard.server as srv
    monkeypatch.setattr(srv, "EVENTS_PATH", events_file)
    # Reset the in-process cache so each test gets a fresh read
    srv._search_cache["key"] = None
    srv._search_cache["events"] = []
    yield events_file


@pytest.fixture()
def client(_patch_events_path):
    """Return a TestClient with a clean app instance."""
    from dashboard.server import app
    return TestClient(app, raise_server_exceptions=True)


def _write_events(path: Path, events: list[dict]) -> None:
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _ev(ts: float, hit: bool = False, keyword: str = "") -> dict:
    return {
        "id":       f"test-{int(ts)}",
        "ts":       ts,
        "ts_iso":   "2024-01-15T12:00:00",
        "duration": 1.5,
        "text":     "test text",
        "hit":      hit,
        "keyword":  keyword if hit else None,
        "excerpt":  "test",
        "audio_file": None,
    }


# ---------------------------------------------------------------------------
# /api/catch-up
# ---------------------------------------------------------------------------

class TestCatchUpEndpoint:

    def test_missing_since_returns_empty(self, client):
        r = client.get("/api/catch-up")
        assert r.status_code == 200
        data = r.json()
        assert data["total_events"] == 0
        assert data["keyword_hits"] == 0
        assert data["hit_events"] == []
        assert data["first_event_time"] is None
        assert data["latest_event_time"] is None

    def test_missing_since_returns_last_10_hits(self, client, _patch_events_path):
        base = time.time() - 1000
        _write_events(_patch_events_path, [
            _ev(base + i, hit=True, keyword="moss lake")
            for i in range(12)
        ])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get("/api/catch-up")
        assert r.status_code == 200
        data = r.json()
        assert data["keyword_hits"] == 10
        assert len(data["hit_events"]) == 10

    def test_empty_since_returns_empty(self, client):
        r = client.get("/api/catch-up?since=")
        assert r.status_code == 200
        assert r.json()["total_events"] == 0

    def test_invalid_since_returns_422(self, client):
        r = client.get("/api/catch-up?since=not-a-date")
        assert r.status_code == 422

    def test_no_events_after_since(self, client, _patch_events_path):
        now = time.time()
        _write_events(_patch_events_path, [_ev(now - 100)])
        # Invalidate server cache
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={now}")  # since = now → no events after
        assert r.status_code == 200
        assert r.json()["total_events"] == 0

    def test_events_after_since_counted(self, client, _patch_events_path):
        base = time.time() - 1000
        _write_events(_patch_events_path, [
            _ev(base + 100),   # before since
            _ev(base + 200),   # after since
            _ev(base + 300),   # after since
        ])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        since = base + 150
        r = client.get(f"/api/catch-up?since={since}")
        assert r.status_code == 200
        data = r.json()
        assert data["total_events"] == 2
        assert data["keyword_hits"] == 0

    def test_keyword_hits_counted_correctly(self, client, _patch_events_path):
        base = time.time() - 500
        _write_events(_patch_events_path, [
            _ev(base + 100, hit=False),
            _ev(base + 200, hit=True,  keyword="moss lake"),
            _ev(base + 300, hit=True,  keyword="moss lake"),
            _ev(base + 400, hit=False),
        ])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={base}")
        data = r.json()
        assert data["total_events"] == 4
        assert data["keyword_hits"] == 2

    def test_hit_events_payload(self, client, _patch_events_path):
        base = time.time() - 500
        _write_events(_patch_events_path, [
            _ev(base + 100, hit=True, keyword="moss lake"),
        ])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={base}")
        data = r.json()
        assert len(data["hit_events"]) == 1
        assert data["hit_events"][0]["keyword"] == "moss lake"

    def test_first_and_latest_event_time_present(self, client, _patch_events_path):
        base = time.time() - 500
        _write_events(_patch_events_path, [
            _ev(base + 100),
            _ev(base + 200),
            _ev(base + 300),
        ])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={base}")
        data = r.json()
        assert data["first_event_time"] is not None
        assert data["latest_event_time"] is not None
        # first should be chronologically earlier
        assert data["first_event_time"] <= data["latest_event_time"]

    def test_since_as_iso_string(self, client, _patch_events_path):
        """Accept ISO-8601 string as the since parameter."""
        base = time.time() - 500
        _write_events(_patch_events_path, [_ev(base + 100)])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        from datetime import datetime
        # A datetime clearly in the past so our event is "after" it
        iso = datetime.fromtimestamp(base - 10).isoformat()
        r = client.get(f"/api/catch-up?since={iso}")
        assert r.status_code == 200
        assert r.json()["total_events"] == 1

    def test_since_unix_float_string(self, client, _patch_events_path):
        """Accept a Unix float as a string."""
        base = time.time() - 500
        _write_events(_patch_events_path, [_ev(base + 100)])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={base - 10:.3f}")
        assert r.status_code == 200
        assert r.json()["total_events"] == 1

    def test_single_event_same_first_and_latest(self, client, _patch_events_path):
        base = time.time() - 500
        _write_events(_patch_events_path, [_ev(base + 100)])
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={base}")
        data = r.json()
        assert data["total_events"] == 1
        assert data["first_event_time"] == data["latest_event_time"]

    def test_hit_events_capped_at_200(self, client, _patch_events_path):
        """hit_events list is capped at 200 entries."""
        base = time.time() - 5000
        events = [_ev(base + i, hit=True, keyword="test") for i in range(1, 260)]
        _write_events(_patch_events_path, events)
        import dashboard.server as srv
        srv._search_cache["key"] = None
        r = client.get(f"/api/catch-up?since={base}")
        data = r.json()
        assert data["keyword_hits"] == 259
        assert len(data["hit_events"]) <= 200


# ---------------------------------------------------------------------------
# /api/last-seen (stub)
# ---------------------------------------------------------------------------

class TestLastSeenEndpoint:

    def test_post_returns_ok(self, client):
        ts = time.time()
        r = client.post("/api/last-seen", json={"timestamp": ts})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert abs(data["last_seen"] - ts) < 0.001

    def test_missing_body_returns_422(self, client):
        r = client.post("/api/last-seen", json={})
        assert r.status_code == 422

    def test_non_numeric_timestamp_returns_422(self, client):
        r = client.post("/api/last-seen", json={"timestamp": "not-a-number"})
        assert r.status_code == 422
