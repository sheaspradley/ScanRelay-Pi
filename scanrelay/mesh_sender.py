"""
Send a text alert over Meshtastic channel index 2.

Uses the official `meshtastic` Python package, which speaks the same TCP API
as the CLI. We hold a single TCPInterface for the life of the daemon and
reconnect on failure.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .config import MeshtasticConfig

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    ok: bool
    error: str = ""


class MeshSender:
    def __init__(self, cfg: MeshtasticConfig):
        self.cfg = cfg
        self._iface = None
        self._lock = threading.Lock()
        self._last_send = 0.0

    def _connect(self) -> None:
        """Lazy connect / reconnect to meshtasticd."""
        if self._iface is not None:
            return
        try:
            # Imported lazily so the package is optional during tests.
            from meshtastic.tcp_interface import TCPInterface  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "meshtastic python package not installed. "
                "pip install meshtastic"
            ) from e

        log.info("Connecting to meshtasticd at %s:%d", self.cfg.host, self.cfg.port)
        self._iface = TCPInterface(hostname=self.cfg.host, portNumber=self.cfg.port)

    def close(self) -> None:
        with self._lock:
            if self._iface is not None:
                try:
                    self._iface.close()
                except Exception:
                    pass
                self._iface = None

    def send_text(self, text: str) -> SendResult:
        """
        Send a single text message on configured channel index.

        Trims to max_text_bytes (UTF-8). Enforces min_send_interval to keep
        the airwaves polite.
        """
        text = self._fit(text)
        with self._lock:
            # Rate limit.
            elapsed = time.time() - self._last_send
            if elapsed < self.cfg.min_send_interval_seconds:
                sleep_for = self.cfg.min_send_interval_seconds - elapsed
                log.debug("Rate limit: sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)

            try:
                self._connect()
                assert self._iface is not None
                self._iface.sendText(
                    text,
                    channelIndex=self.cfg.channel_index,
                    wantAck=False,
                )
                self._last_send = time.time()
                log.info("Sent (%d bytes): %s", len(text.encode("utf-8")), text)
                return SendResult(ok=True)
            except Exception as e:
                log.error("Send failed: %s", e)
                # Drop the interface so next call reconnects.
                try:
                    if self._iface is not None:
                        self._iface.close()
                except Exception:
                    pass
                self._iface = None
                return SendResult(ok=False, error=str(e))

    def _fit(self, text: str) -> str:
        """Trim text to max_text_bytes in UTF-8, ending on a word boundary if possible."""
        max_b = self.cfg.max_text_bytes
        encoded = text.encode("utf-8")
        if len(encoded) <= max_b:
            return text
        # Walk back to a space within ~20 bytes of the limit so we don't cut a word.
        cut = encoded[:max_b]
        try:
            decoded = cut.decode("utf-8", errors="ignore")
        except Exception:
            decoded = text[: max_b // 2]
        for sep in (" ", ",", "."):
            i = decoded.rfind(sep, max(0, len(decoded) - 20))
            if i > 0:
                return decoded[:i] + "\u2026"
        return decoded + "\u2026"
