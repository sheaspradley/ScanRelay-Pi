"""ntfy.sh push sender for ScanRelay."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from .config import NtfyConfig


@dataclass
class NtfyResult:
    ok: bool
    error: str | None = None
    status_code: int = 0


class NtfySender:
    def __init__(self, cfg: NtfyConfig):
        self.cfg = cfg

    def send(
        self,
        title: str,
        message: str,
        priority: int = 3,
        tags: list[str] | None = None,
        attach_path: Path | None = None,
    ) -> NtfyResult:
        """Send a push notification.

        Uses a short timeout so ntfy cannot stall scanner processing.
        """
        if not self.cfg.enabled:
            return NtfyResult(ok=True, status_code=0)
        topic = self.cfg.topic.strip()
        if not topic:
            return NtfyResult(ok=False, error="ntfy topic is empty", status_code=0)

        server = self.cfg.server.rstrip("/") or "https://ntfy.sh"
        url = f"{server}/{topic}"
        tag_text = ",".join(tags or [])
        headers = {
            "X-Title": title,
            "X-Priority": str(max(1, min(5, int(priority)))),
        }
        if tag_text:
            headers["X-Tags"] = tag_text

        try:
            if attach_path is not None:
                headers["X-Filename"] = attach_path.name
                with open(attach_path, "rb") as fh:
                    resp = requests.put(url, data=fh, headers=headers, timeout=3)
            else:
                resp = requests.put(
                    url,
                    data=message.encode("utf-8"),
                    headers=headers,
                    timeout=3,
                )
            if 200 <= resp.status_code < 300:
                return NtfyResult(ok=True, status_code=resp.status_code)
            return NtfyResult(
                ok=False,
                error=f"ntfy returned HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        except Exception as e:
            return NtfyResult(ok=False, error=str(e), status_code=0)
