"""
AI-powered daily narrative summary for ScanRelay.

Reads the day's events (list of dicts from events.jsonl) and calls the
OpenAI Chat Completions API to produce a single paragraph that a
non-technical reader can understand.

Usage
-----
    from dashboard.ai_summary import generate_ai_summary
    from scanrelay.config import AISummaryConfig

    paragraph = generate_ai_summary(today_events, cfg.ai_summary)
    if paragraph:
        print(paragraph)

__main__ block lets you test from the CLI:
    python -m dashboard.ai_summary /var/lib/scanrelay/logs/events.jsonl 2024-01-15
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scanrelay.config import AISummaryConfig

log = logging.getLogger(__name__)


def _resolve_api_key(cfg: "AISummaryConfig") -> str | None:
    """Return the API key: env var wins over literal config value."""
    # Environment variable takes priority (safer for secrets).
    env_key = os.environ.get(cfg.api_key_env, "").strip()
    if env_key:
        return env_key
    literal = cfg.api_key.strip()
    return literal if literal else None


def _build_transcript_block(events: list[dict], max_chars: int) -> str:
    """Build a chronological transcript for the prompt, truncated to max_chars."""
    # Sort ascending by timestamp.
    sorted_evs = sorted(events, key=lambda e: e.get("ts", 0))

    lines: list[str] = []
    total = 0
    for ev in sorted_evs:
        text = (ev.get("text") or "").strip()
        if not text:
            continue
        ts = ev.get("ts", 0)
        try:
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
        except (ValueError, OSError):
            t = "??"
        hit_marker = " [MATCH]" if ev.get("hit") else ""
        lang_info = ""
        if ev.get("language") and ev["language"] != "en":
            lang_info = f" [{ev['language']}→en]" if ev.get("translated") else f" [{ev['language']}]"
        line = f"{t}{hit_marker}{lang_info}: {text}"
        # Check if adding this line would exceed the budget.
        if total + len(line) + 1 > max_chars:
            lines.append(f"[...truncated at {max_chars} chars...]")
            break
        lines.append(line)
        total += len(line) + 1  # +1 for newline

    return "\n".join(lines)


def generate_ai_summary(
    events: list[dict],
    cfg: "AISummaryConfig",
) -> str | None:
    """Generate a narrative paragraph summarising today's scanner traffic.

    Parameters
    ----------
    events:  List of event dicts (already filtered to the target day).
    cfg:     AISummaryConfig instance from the loaded Config.

    Returns
    -------
    A paragraph string, or None on any failure (errors are logged, never raised).
    """
    if not cfg.enabled:
        return None

    if not events:
        log.info("ai_summary: no events to summarise")
        return None

    api_key = _resolve_api_key(cfg)
    if not api_key:
        log.warning(
            "ai_summary: no API key found (set %s env var or api_key in config)",
            cfg.api_key_env,
        )
        return None

    # Build transcript block.
    transcript_block = _build_transcript_block(events, cfg.max_input_chars)
    if not transcript_block.strip():
        log.info("ai_summary: transcript block is empty after filtering")
        return None

    total_events = len(events)
    hit_count = sum(1 for e in events if e.get("hit"))

    user_message = (
        f"Below is a chronological log of scanner radio transmissions from today "
        f"({total_events} total transmissions, {hit_count} keyword hits). "
        f"Lines marked [MATCH] triggered a keyword alert.\n\n"
        f"{transcript_block}"
    )

    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": cfg.style},
            {"role": "user",   "content": user_message},
        ],
        "max_tokens": 300,
        "temperature": 0.4,
    }

    log.info(
        "ai_summary: calling %s model=%s input_chars=%d",
        cfg.provider, cfg.model, len(user_message),
    )

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        response = json.loads(raw)
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        log.warning("ai_summary: HTTP %d from OpenAI: %s", exc.code, error_body)
        return None
    except Exception as exc:
        log.warning("ai_summary: request failed: %s", exc)
        return None

    try:
        paragraph = response["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("ai_summary: unexpected response shape: %s — %r", exc, str(response)[:200])
        return None

    if not paragraph:
        log.warning("ai_summary: empty response from model")
        return None

    log.info("ai_summary: generated %d-char paragraph", len(paragraph))
    return paragraph


# ---------------------------------------------------------------------------
# CLI — test against a real events.jsonl
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print(
            "Usage: python -m dashboard.ai_summary <events.jsonl> [YYYY-MM-DD]",
            file=sys.stderr,
        )
        sys.exit(1)

    events_path = Path(sys.argv[1])
    if not events_path.exists():
        print(f"File not found: {events_path}", file=sys.stderr)
        sys.exit(1)

    target_date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().date().isoformat()
    try:
        dt = datetime.fromisoformat(target_date)
    except ValueError:
        print(f"Invalid date: {target_date}", file=sys.stderr)
        sys.exit(1)

    day_start = datetime.combine(dt.date(), datetime.min.time()).timestamp()
    day_end   = day_start + 86400

    all_events: list[dict] = []
    with open(events_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if day_start <= ev.get("ts", 0) < day_end:
                    all_events.append(ev)
            except json.JSONDecodeError:
                pass

    print(f"Loaded {len(all_events)} events for {target_date}")

    # Build a minimal AISummaryConfig from environment / defaults.
    # Avoid importing from scanrelay package if not installed.
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scanrelay.config import AISummaryConfig
        cfg = AISummaryConfig(enabled=True)
    except ImportError:
        # Fallback: inline dataclass-like object.
        class AISummaryConfig:  # type: ignore[no-redef]
            enabled = True
            provider = "openai"
            model = "gpt-4o-mini"
            api_key = ""
            api_key_env = "OPENAI_API_KEY"
            max_input_chars = 12000
            style = (
                "Write a single tight paragraph (3-5 sentences) summarizing what happened on "
                "the scanner today for a non-technical reader."
            )
        cfg = AISummaryConfig()  # type: ignore[assignment]

    result = generate_ai_summary(all_events, cfg)
    if result:
        print("\n--- AI Summary ---")
        print(result)
    else:
        print("No summary generated (check API key / logs).")
