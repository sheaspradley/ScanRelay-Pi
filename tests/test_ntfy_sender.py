from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanrelay.config import NtfyConfig
from scanrelay.ntfy_sender import NtfySender


class Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_text_push_uses_put_and_headers(monkeypatch):
    calls = []
    def fake_put(url, data=None, headers=None, timeout=None):
        calls.append((url, data, headers, timeout))
        return Resp(200)
    monkeypatch.setattr("scanrelay.ntfy_sender.requests.put", fake_put)

    result = NtfySender(NtfyConfig(enabled=True, topic="abc")).send(
        "Title", "hello", priority=5, tags=["siren", "radio"]
    )

    assert result.ok is True
    url, data, headers, timeout = calls[0]
    assert url == "https://ntfy.sh/abc"
    assert data == b"hello"
    assert headers["X-Title"] == "Title"
    assert headers["X-Priority"] == "5"
    assert headers["X-Tags"] == "siren,radio"
    assert timeout == 3


def test_attachment_push_sends_file_body(monkeypatch, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"mp3")
    seen = {}
    def fake_put(url, data=None, headers=None, timeout=None):
        seen["body"] = data.read()
        seen["headers"] = headers
        return Resp(201)
    monkeypatch.setattr("scanrelay.ntfy_sender.requests.put", fake_put)

    result = NtfySender(NtfyConfig(enabled=True, topic="abc")).send(
        "Title", "ignored", attach_path=audio
    )

    assert result.ok is True
    assert seen["body"] == b"mp3"
    assert seen["headers"]["X-Filename"] == "clip.mp3"


def test_http_error_returns_result(monkeypatch):
    monkeypatch.setattr("scanrelay.ntfy_sender.requests.put", lambda *a, **k: Resp(500))
    result = NtfySender(NtfyConfig(enabled=True, topic="abc")).send("t", "m")
    assert result.ok is False
    assert result.status_code == 500
