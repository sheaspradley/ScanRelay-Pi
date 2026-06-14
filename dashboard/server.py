"""
ScanRelay Web Dashboard v3 — FastAPI backend.

Endpoints (backward-compatible superset of v2):
  GET  /                      - SPA (index.html)
  GET  /api/events            - recent events (limit, hits_only, since, search) + category field
  GET  /api/stream            - SSE live tail
  GET  /api/stats             - counters
  GET  /api/summary           - 7-day chart + keyword breakdown
  GET  /api/export.csv        - CSV download
  GET  /api/audio/<fname>     - serve WAV
  GET  /api/config            - full TOML config as JSON
  PUT  /api/config            - save config (validate + write TOML)
  POST /api/match_all         - toggle filter.match_all + restart daemon
  GET  /api/search            - full-text search over entire events.jsonl
  GET  /api/health            - system health metrics (cpu, mem, disk, etc.)
  GET  /api/version           - OTA: current sha, latest remote, behind count
  POST /api/update            - OTA: git pull + service restart
  GET  /api/dvr/timeline      - last-hour clip timeline
  GET  /api/summaries         - list summary markdown files
  GET  /api/summaries/{date}  - single daily summary

Run:
  uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator, Any

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Paths (override via env for testing).
EVENTS_PATH = Path(os.environ.get("SCANRELAY_EVENTS",
                                  "/var/lib/scanrelay/logs/events.jsonl"))
AUDIO_DIR   = Path(os.environ.get("SCANRELAY_AUDIO",
                                  "/var/lib/scanrelay/audio"))
CONFIG_PATH = Path(os.environ.get("SCANRELAY_CONFIG",
                                  "/etc/scanrelay/scanrelay.toml"))
STATIC_DIR  = Path(__file__).parent / "static"
SUMMARIES_DIR = Path(os.environ.get("SCANRELAY_SUMMARIES",
                                    "/var/lib/scanrelay/summaries"))
GEOCACHE_PATH = Path(os.environ.get("SCANRELAY_GEOCACHE",
                                    "/var/lib/scanrelay/geocache.json"))

SCANRELAY_UNIT = os.environ.get("SCANRELAY_UNIT", "scanrelay.service")
DASHBOARD_START = time.time()

# ---------------------------------------------------------------------------
app = FastAPI(title="ScanRelay Dashboard v3")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Import categorizer (works from both repo root and installed paths)
try:
    from scanrelay.categorize import categorize_event
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from scanrelay.categorize import categorize_event
    except ImportError:
        def categorize_event(ev: dict) -> str:  # type: ignore[misc]
            return "UNKNOWN"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_events(limit: int = 200, hits_only: bool = False,
                 since: float | None = None,
                 search: str | None = None) -> list[dict]:
    """Read the last N events from events.jsonl. Returns newest-first."""
    if not EVENTS_PATH.exists():
        return []
    if since is not None:
        chunk = 4 * 1024 * 1024
    else:
        chunk = max(limit * 2000, 64 * 1024)
    with open(EVENTS_PATH, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - chunk))
        tail = f.read().decode("utf-8", errors="replace")
    out: list[dict] = []
    needle = search.lower() if search else None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if hits_only and not ev.get("hit"):
            continue
        if since is not None and ev.get("ts", 0) < since:
            continue
        if needle and needle not in (ev.get("text") or "").lower():
            continue
        ev["category"] = categorize_event(ev)
        out.append(ev)
    out.reverse()
    return out[:limit]


def _file_inode_size(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_ino, st.st_size
    except FileNotFoundError:
        return 0, 0


# ---------------------------------------------------------------------------
# Full-file cache for search (avoid re-parsing unchanged file)
# ---------------------------------------------------------------------------
_search_cache: dict[str, Any] = {"key": None, "events": []}

def _read_all_events_cached() -> list[dict]:
    """Read and parse all events.jsonl, cached by (path, mtime)."""
    try:
        st = EVENTS_PATH.stat()
        key = (str(EVENTS_PATH), st.st_mtime, st.st_size)
    except FileNotFoundError:
        return []
    if _search_cache["key"] == key:
        return _search_cache["events"]
    events: list[dict] = []
    with open(EVENTS_PATH, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ev["category"] = categorize_event(ev)
                events.append(ev)
            except json.JSONDecodeError:
                continue
    events.reverse()  # newest-first
    _search_cache["key"] = key
    _search_cache["events"] = events
    return events


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

async def _event_stream() -> AsyncGenerator[bytes, None]:
    """SSE stream that yields every new line appended to events.jsonl."""
    while not EVENTS_PATH.exists():
        await asyncio.sleep(1.0)
    inode, pos = _file_inode_size(EVENTS_PATH)
    f = open(EVENTS_PATH, "r")
    f.seek(pos)
    try:
        yield b": connected\n\n"
        last_ping = time.monotonic()
        while True:
            line = f.readline()
            if line:
                line = line.strip()
                if line:
                    try:
                        ev = json.loads(line)
                        ev["category"] = categorize_event(ev)
                        yield f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                    except json.JSONDecodeError:
                        pass
                continue
            new_inode, new_size = _file_inode_size(EVENTS_PATH)
            if new_inode != inode or new_size < pos:
                f.close()
                f = open(EVENTS_PATH, "r")
                inode, pos = new_inode, 0
                continue
            pos = f.tell()
            if time.monotonic() - last_ping > 15:
                yield b": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(0.4)
    finally:
        f.close()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_toml() -> dict:
    """Load the TOML config, return as dict. Falls back to {} on any error."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _write_toml(data: dict) -> None:
    """Write config back as TOML. Tries tomli_w, falls back to manual."""
    try:
        import tomli_w
        text = tomli_w.dumps(data)
    except ImportError:
        # Manual serializer (handles nested dicts + lists of strings/numbers/bools)
        lines: list[str] = [
            "# ScanRelay configuration — auto-saved by dashboard v3\n"
        ]
        def _write_section(section_dict: dict, prefix: str = "") -> None:
            scalars = {k: v for k, v in section_dict.items() if not isinstance(v, dict)}
            tables  = {k: v for k, v in section_dict.items() if isinstance(v, dict)}
            for k, v in scalars.items():
                if isinstance(v, bool):
                    lines.append(f"{k} = {'true' if v else 'false'}\n")
                elif isinstance(v, str):
                    lines.append(f"{k} = {json.dumps(v)}\n")
                elif isinstance(v, list):
                    items = ", ".join(json.dumps(x) if isinstance(x, str) else str(x) for x in v)
                    lines.append(f"{k} = [{items}]\n")
                else:
                    lines.append(f"{k} = {v}\n")
            for k, v in tables.items():
                full_key = f"{prefix}.{k}" if prefix else k
                lines.append(f"\n[{full_key}]\n")
                _write_section(v, full_key)
        _write_section(data)
        text = "".join(lines)

    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text(text)
    shutil.move(str(tmp), str(CONFIG_PATH))


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    p = STATIC_DIR / "index.html"
    if not p.exists():
        return JSONResponse({"error": "index.html not found"}, status_code=500)
    return FileResponse(str(p))


# PWA v3.1 — serve SW and manifest from root so SW has root scope
@app.get("/sw.js")
async def serve_sw():
    sw_path = STATIC_DIR / "sw.js"
    if not sw_path.exists():
        return JSONResponse({"error": "sw.js not found"}, status_code=404)
    return Response(
        sw_path.read_text(),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache, no-store"},
    )


@app.get("/manifest.webmanifest")
async def serve_manifest():
    mf_path = STATIC_DIR / "manifest.webmanifest"
    if not mf_path.exists():
        return JSONResponse({"error": "manifest.webmanifest not found"}, status_code=404)
    return Response(
        mf_path.read_text(),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/events")
def api_events(limit: int = 200, hits_only: bool = False,
               since: float | None = None, search: str | None = None):
    return JSONResponse(_read_events(limit=limit, hits_only=hits_only,
                                     since=since, search=search))


@app.get("/api/summary")
def api_summary():
    """7-day chart data + per-keyword breakdown for today."""
    now = time.time()
    seven_days_ago = now - 7 * 86400
    events = _read_events(limit=20000, since=seven_days_ago)

    today_local = datetime.now().astimezone().date()
    days: list[dict] = []
    for i in range(6, -1, -1):
        d = today_local - timedelta(days=i)
        days.append({"date": d.isoformat(), "total": 0, "hits": 0})
    day_idx = {d["date"]: i for i, d in enumerate(days)}

    today_keywords: Counter[str] = Counter()
    for ev in events:
        try:
            d = datetime.fromtimestamp(ev.get("ts", 0)).astimezone().date().isoformat()
        except (ValueError, OSError):
            continue
        if d in day_idx:
            days[day_idx[d]]["total"] += 1
            if ev.get("hit"):
                days[day_idx[d]]["hits"] += 1
        if d == today_local.isoformat() and ev.get("hit") and ev.get("keyword"):
            today_keywords[ev["keyword"]] += 1

    return {
        "days": days,
        "today_keywords": [{"keyword": k, "count": c}
                           for k, c in today_keywords.most_common()],
    }


@app.get("/api/export.csv")
def api_export_csv(scope: str = "today"):
    if scope == "today":
        midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        events = _read_events(limit=50000, since=midnight)
        fname = f"scanrelay-{datetime.now().strftime('%Y%m%d')}.csv"
    else:
        events = _read_events(limit=50000)
        fname = "scanrelay-recent.csv"

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "local_time", "duration_s", "hit",
                "keyword", "category", "transcript", "audio_file"])
    for ev in events:
        ts = ev.get("ts", 0)
        local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
        w.writerow([
            ts, local, ev.get("duration", ""),
            "1" if ev.get("hit") else "0",
            ev.get("keyword") or "",
            ev.get("category", "UNKNOWN"),
            (ev.get("text") or "").replace("\n", " "),
            ev.get("audio_file") or "",
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/stream")
async def api_stream():
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/stats")
def api_stats():
    events = _read_events(limit=5000)
    now = time.time()
    one_day = 24 * 3600
    today = [e for e in events if now - e.get("ts", 0) < one_day]
    hits_today = [e for e in today if e.get("hit")]
    last_hit_ts = max((e.get("ts", 0) for e in events if e.get("hit")), default=None)

    uptime_seconds: float | None = None
    try:
        out = subprocess.check_output(
            ["systemctl", "show", SCANRELAY_UNIT,
             "--property=ActiveEnterTimestampMonotonic,ActiveState"],
            text=True, timeout=2,
        )
        props = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        if props.get("ActiveState") == "active":
            mono_us = int(props.get("ActiveEnterTimestampMonotonic", "0"))
            if mono_us > 0:
                with open("/proc/uptime") as pf:
                    sys_uptime = float(pf.read().split()[0])
                uptime_seconds = sys_uptime - (mono_us / 1_000_000)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass

    audio_mb = 0.0
    audio_count = 0
    if AUDIO_DIR.exists():
        for p in AUDIO_DIR.glob("*.wav"):
            try:
                audio_mb += p.stat().st_size / (1024 * 1024)
                audio_count += 1
            except FileNotFoundError:
                pass

    return {
        "total_today": len(today),
        "hits_today": len(hits_today),
        "total_all": len(events),
        "last_hit_ts": last_hit_ts,
        "uptime_seconds": uptime_seconds,
        "audio_files": audio_count,
        "audio_mb": round(audio_mb, 1),
    }


@app.get("/api/audio/{fname}")
def api_audio(fname: str):
    if "/" in fname or ".." in fname or not fname.endswith(".wav"):
        raise HTTPException(status_code=400, detail="bad filename")
    p = AUDIO_DIR / fname
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(p), media_type="audio/wav")


# ---------------------------------------------------------------------------
# Config (v3 — full TOML read/write)
# ---------------------------------------------------------------------------

@app.get("/api/config")
def api_config():
    """Return full TOML config as JSON, including v3 sections."""
    data = _load_toml()

    # Inject defaults for v3 sections if missing
    data.setdefault("filter", {})
    data.setdefault("ntfy", {"enabled": False, "topic": "", "priority": 4})
    data.setdefault("map", {
        "default_lat": 39.5,
        "default_lon": -98.35,
        "default_zoom": 4,
    })
    data.setdefault("quiet_hours", {"enabled": False, "start": "22:00", "end": "06:00"})
    data.setdefault("summary", {
        "enabled": True,
        "time": "18:00",
        "timezone": "America/Chicago",
        "ntfy_push": False,
    })
    return data


class ConfigBody(BaseModel):
    data: dict


@app.put("/api/config")
def api_config_put(body: ConfigBody):
    """Validate and write config. Returns {ok, restart_required}."""
    incoming = body.data

    # Basic validation
    filt = incoming.get("filter", {})
    kws = filt.get("keywords", [])
    if not isinstance(kws, list):
        raise HTTPException(status_code=422, detail="filter.keywords must be a list")
    if not all(isinstance(k, str) for k in kws):
        raise HTTPException(status_code=422, detail="filter.keywords must be strings")

    ntfy = incoming.get("ntfy", {})
    if ntfy.get("enabled") and not ntfy.get("topic", "").strip():
        raise HTTPException(status_code=422, detail="ntfy.topic required when enabled")

    # Determine restart_required (any daemon-side section changed)
    current = _load_toml()
    daemon_sections = {"filter", "whisper", "vad", "audio", "mesh"}
    restart_required = any(
        incoming.get(s) != current.get(s) for s in daemon_sections
    )

    try:
        _write_toml(incoming)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")

    return {"ok": True, "restart_required": restart_required}


class MatchAllBody(BaseModel):
    enabled: bool


@app.post("/api/match_all")
def api_match_all(body: MatchAllBody):
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=500, detail="config not found")
    text = CONFIG_PATH.read_text()
    new_val = "true" if body.enabled else "false"
    if re.search(r"(?m)^match_all\s*=", text):
        new_text = re.sub(r"(?m)^match_all\s*=.*$", f"match_all = {new_val}", text)
    else:
        if re.search(r"(?m)^dedup_window_seconds\s*=", text):
            new_text = re.sub(
                r"(?m)^(dedup_window_seconds\s*=.*)$",
                rf"\1\nmatch_all = {new_val}", text, count=1,
            )
        else:
            new_text = re.sub(
                r"(?m)^(\[filter\])", rf"\1\nmatch_all = {new_val}", text, count=1,
            )
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text(new_text)
    shutil.move(str(tmp), str(CONFIG_PATH))

    restart_ok = False
    restart_err = None
    try:
        subprocess.run(
            ["systemctl", "restart", SCANRELAY_UNIT],
            check=True, capture_output=True, text=True, timeout=15,
        )
        restart_ok = True
    except subprocess.CalledProcessError as e:
        restart_err = e.stderr or str(e)
    except FileNotFoundError:
        restart_err = "systemctl not available"

    return {"match_all": body.enabled, "restart_ok": restart_ok, "restart_error": restart_err}


# ---------------------------------------------------------------------------
# Feature 3: Full-text search
# ---------------------------------------------------------------------------

@app.get("/api/search")
def api_search(q: str = "", from_: str | None = None, to: str | None = None,
               limit: int = 500):
    """Full-text search over entire events.jsonl."""
    if not q.strip():
        return {"results": [], "total": 0, "days": 0, "query": q}

    needle = q.strip().lower()
    from_ts: float | None = None
    to_ts: float | None = None
    try:
        if from_:
            from_ts = datetime.fromisoformat(from_).timestamp()
        if to:
            to_ts = datetime.fromisoformat(to).timestamp()
    except ValueError:
        pass

    all_events = _read_all_events_cached()
    results = []
    dates: set[str] = set()
    for ev in all_events:
        if needle not in (ev.get("text") or "").lower():
            continue
        ts = ev.get("ts", 0)
        if from_ts and ts < from_ts:
            continue
        if to_ts and ts > to_ts:
            continue
        results.append(ev)
        try:
            dates.add(datetime.fromtimestamp(ts).date().isoformat())
        except (ValueError, OSError):
            pass

    return {
        "results": results[:limit],
        "total": len(results),
        "days": len(dates),
        "query": q,
    }


# ---------------------------------------------------------------------------
# Feature 4: System health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    """System health metrics."""
    result: dict = {}

    # CPU % — try psutil, fall back to /proc/stat two-sample (simplified single-shot)
    cpu_pct: float | None = None
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.1)
    except ImportError:
        pass
    result["cpu_percent"] = cpu_pct

    # CPU temp
    cpu_temp: float | None = None
    try:
        t = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        cpu_temp = int(t) / 1000.0
    except Exception:
        try:
            import psutil
            temps = psutil.sensors_temperatures()
            for zone in temps.values():
                if zone:
                    cpu_temp = zone[0].current
                    break
        except Exception:
            pass
    result["cpu_temp_c"] = cpu_temp

    # Memory
    mem_used: float | None = None
    mem_total: float | None = None
    try:
        import psutil
        vm = psutil.virtual_memory()
        mem_used  = round(vm.used  / 1024 / 1024, 1)
        mem_total = round(vm.total / 1024 / 1024, 1)
    except ImportError:
        try:
            with open("/proc/meminfo") as f:
                mi = dict(line.split(":") for line in f if ":" in line)
            def _kb(key: str) -> int:
                return int(mi.get(key, "0").strip().split()[0])
            mem_total = round(_kb("MemTotal") / 1024, 1)
            mem_used  = round(((_kb("MemTotal") - _kb("MemAvailable")) / 1024), 1)
        except Exception:
            pass
    result["mem_used_mb"]  = mem_used
    result["mem_total_mb"] = mem_total

    # Disk
    try:
        du = shutil.disk_usage(str(AUDIO_DIR.parent) if AUDIO_DIR.exists() else "/")
        result["disk_used_gb"]  = round(du.used  / 1e9, 2)
        result["disk_total_gb"] = round(du.total / 1e9, 2)
    except Exception:
        result["disk_used_gb"]  = None
        result["disk_total_gb"] = None

    # Audio dir size
    audio_mb = 0.0
    if AUDIO_DIR.exists():
        for p in AUDIO_DIR.rglob("*"):
            try:
                audio_mb += p.stat().st_size / 1e6
            except Exception:
                pass
    result["audio_dir_mb"] = round(audio_mb, 2)

    # Events count + queue depth
    events_count = 0
    queue_depth = 0
    if EVENTS_PATH.exists():
        with open(EVENTS_PATH, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    events_count += 1
                    if not ev.get("audio_file"):
                        queue_depth += 1
                except json.JSONDecodeError:
                    pass
    result["events_count"]      = events_count
    result["queue_depth_estimate"] = queue_depth

    # Daemon uptime
    daemon_uptime: float | None = None
    try:
        out = subprocess.check_output(
            ["systemctl", "show", SCANRELAY_UNIT,
             "--property=ActiveEnterTimestampMonotonic,ActiveState"],
            text=True, timeout=2,
        )
        props = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        if props.get("ActiveState") == "active":
            mono_us = int(props.get("ActiveEnterTimestampMonotonic", "0"))
            if mono_us > 0:
                with open("/proc/uptime") as pf:
                    sys_uptime = float(pf.read().split()[0])
                daemon_uptime = sys_uptime - (mono_us / 1_000_000)
    except Exception:
        pass
    result["daemon_uptime_seconds"]    = daemon_uptime
    result["dashboard_uptime_seconds"] = time.time() - DASHBOARD_START

    # Last event ago
    last_ts: float | None = None
    if EVENTS_PATH.exists():
        try:
            with open(EVENTS_PATH, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    last_ts = ev.get("ts")
                    if last_ts:
                        break
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
    result["last_event_ago_seconds"] = (
        round(time.time() - last_ts, 1) if last_ts else None
    )

    return result


# ---------------------------------------------------------------------------
# Feature 6: OTA updates
# ---------------------------------------------------------------------------

INSTALL_DIR = Path(os.environ.get("SCANRELAY_INSTALL", "/opt/scanrelay"))

def _git_cmd(*args: str) -> tuple[bool, str]:
    """Run git command in INSTALL_DIR. Returns (success, stdout)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(INSTALL_DIR)] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0, r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""


@app.get("/api/version")
def api_version():
    git_dir = INSTALL_DIR / ".git"
    if not git_dir.exists():
        return {
            "current": "tarball",
            "branch": None,
            "latest_remote": None,
            "behind": 0,
            "update_available": False,
            "message": "Install via git clone to enable OTA updates",
        }

    ok, sha = _git_cmd("rev-parse", "--short", "HEAD")
    current = sha if ok else "unknown"

    ok, branch = _git_cmd("rev-parse", "--abbrev-ref", "HEAD")
    branch_name = branch if ok else "unknown"

    # Try to fetch (non-fatal)
    _git_cmd("fetch", "--quiet")

    ok, remote_sha = _git_cmd("rev-parse", f"origin/{branch_name}")
    latest = remote_sha[:8] if ok and remote_sha else None

    behind = 0
    if ok:
        ok2, cnt = _git_cmd("rev-list", "--count", f"HEAD..origin/{branch_name}")
        if ok2:
            try:
                behind = int(cnt)
            except ValueError:
                pass

    return {
        "current": current,
        "branch": branch_name,
        "latest_remote": latest,
        "behind": behind,
        "update_available": behind > 0,
    }


class UpdateBody(BaseModel):
    confirm: str  # must be "yes"


@app.post("/api/update")
def api_update(body: UpdateBody):
    if body.confirm != "yes":
        raise HTTPException(status_code=400, detail="Send confirm=yes to proceed")
    git_dir = INSTALL_DIR / ".git"
    if not git_dir.exists():
        raise HTTPException(status_code=400, detail="Not a git install")

    ok, out = _git_cmd("pull")
    if not ok:
        raise HTTPException(status_code=500, detail=f"git pull failed: {out}")

    # Restart services (non-fatal)
    restart_out = ""
    try:
        r = subprocess.run(
            ["systemctl", "restart", "scanrelay-dashboard", "scanrelay"],
            capture_output=True, text=True, timeout=30,
        )
        restart_out = r.stderr or "ok"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        restart_out = str(e)

    return {"ok": True, "git_output": out, "restart_output": restart_out}


# ---------------------------------------------------------------------------
# Feature 8: DVR timeline
# ---------------------------------------------------------------------------

@app.get("/api/dvr/timeline")
def api_dvr_timeline():
    """Return last-hour events sorted ascending for DVR timeline."""
    cutoff = time.time() - 3600
    events = _read_events(limit=5000, since=cutoff)
    # Sort ascending for timeline
    events = sorted(events, key=lambda e: e.get("ts", 0))
    return [
        {
            "ts": ev.get("ts"),
            "duration": ev.get("duration"),
            "audio_file": ev.get("audio_file"),
            "hit": ev.get("hit", False),
            "text": ev.get("text", ""),
            "category": ev.get("category", "UNKNOWN"),
            "id": ev.get("id"),
        }
        for ev in events
    ]


@app.get("/api/dvr/manifest")
def api_dvr_manifest(from_ts: float | None = None, to_ts: float | None = None):
    """Return a sequential-playback manifest for clips in [from_ts, to_ts]."""
    cutoff = from_ts or (time.time() - 3600)
    events = _read_events(limit=5000, since=cutoff)
    events = sorted(events, key=lambda e: e.get("ts", 0))
    clips = []
    for ev in events:
        if to_ts and ev.get("ts", 0) > to_ts:
            break
        if ev.get("audio_file"):
            clips.append({
                "ts": ev.get("ts"),
                "url": f"/api/audio/{ev['audio_file']}",
                "duration": ev.get("duration", 0),
                "hit": ev.get("hit", False),
                "text": ev.get("text", ""),
            })
    return {"clips": clips, "count": len(clips)}


# ---------------------------------------------------------------------------
# Feature 10: Daily summaries
# ---------------------------------------------------------------------------

def _generate_daily_summary(target_date: str | None = None) -> str:
    """Generate a Markdown daily summary for the given date (YYYY-MM-DD)."""
    if target_date is None:
        target_date = datetime.now().astimezone().date().isoformat()

    try:
        dt = datetime.fromisoformat(target_date)
    except ValueError:
        return f"# {target_date}\n\nInvalid date.\n"

    day_start = datetime.combine(dt.date(), datetime.min.time()).astimezone().timestamp()
    day_end   = day_start + 86400

    all_evs = _read_all_events_cached()
    day_evs  = [e for e in all_evs if day_start <= e.get("ts", 0) < day_end]
    hits     = [e for e in day_evs if e.get("hit")]

    # Category breakdown
    cat_counts: Counter[str] = Counter(e.get("category", "UNKNOWN") for e in day_evs)
    # Keyword breakdown
    kw_counts: Counter[str] = Counter(
        e["keyword"] for e in hits if e.get("keyword")
    )
    # Busiest hour
    hour_counts: Counter[int] = Counter()
    for e in day_evs:
        try:
            hour_counts[datetime.fromtimestamp(e["ts"]).hour] += 1
        except Exception:
            pass
    busiest = hour_counts.most_common(1)
    busiest_str = f"{busiest[0][0]:02d}:00 ({busiest[0][1]} events)" if busiest else "—"

    # Longest hit transcripts
    long_hits = sorted(hits, key=lambda e: len(e.get("text", "")), reverse=True)[:3]

    lines = [
        f"# ScanRelay Daily Summary — {target_date}",
        "",
        f"Today ScanRelay heard **{len(day_evs)} transmissions**, **{len(hits)} hits**.",
        "",
        f"**Busiest hour:** {busiest_str}",
        "",
        "## Top Categories",
    ]
    for cat, cnt in cat_counts.most_common():
        lines.append(f"- {cat}: {cnt}")
    lines.append("")
    if kw_counts:
        lines.append("## Top Keywords")
        for kw, cnt in kw_counts.most_common(5):
            lines.append(f"- `{kw}`: {cnt}")
        lines.append("")
    if long_hits:
        lines.append("## Notable Transcripts")
        for ev in long_hits:
            ts_str = datetime.fromtimestamp(ev["ts"]).strftime("%H:%M:%S") if ev.get("ts") else "?"
            lines.append(f"- **{ts_str}** [{ev.get('keyword', '')}]: {ev.get('text', '')}")
        lines.append("")
    return "\n".join(lines) + "\n"


# Cron state for daily summaries
_summary_task_started = False


async def _daily_summary_cron() -> None:
    """Background asyncio task: generate + save summary daily at configured time."""
    while True:
        try:
            cfg = _load_toml()
            summary_cfg = cfg.get("summary", {})
            if not summary_cfg.get("enabled", True):
                await asyncio.sleep(300)
                continue

            tz_name  = summary_cfg.get("timezone", "America/Chicago")
            run_time = summary_cfg.get("time", "18:00")

            try:
                run_h, run_m = (int(x) for x in run_time.split(":"))
            except ValueError:
                run_h, run_m = 18, 0

            now = datetime.now().astimezone()
            target = now.replace(hour=run_h, minute=run_m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            await asyncio.sleep(min(wait_secs, 3600))  # wake up every hour at minimum
            # Check if we're within 30s of fire time
            now2 = datetime.now().astimezone()
            delta = abs((now2.replace(hour=run_h, minute=run_m, second=0, microsecond=0) - now2).total_seconds())
            if delta > 60:
                continue

            # Generate
            date_str = now2.date().isoformat()
            md = _generate_daily_summary(date_str)
            SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
            out_path = SUMMARIES_DIR / f"{date_str}.md"
            out_path.write_text(md)

            # ntfy push
            ntfy_cfg = cfg.get("ntfy", {})
            if ntfy_cfg.get("enabled") and ntfy_cfg.get("topic") and summary_cfg.get("ntfy_push"):
                try:
                    import urllib.request
                    topic = ntfy_cfg["topic"].strip()
                    req = urllib.request.Request(
                        f"https://ntfy.sh/{topic}",
                        data=md[:1000].encode(),
                        headers={
                            "Title": f"ScanRelay summary {date_str}",
                            "Priority": str(ntfy_cfg.get("priority", 3)),
                            "Tags": "radio,calendar",
                        },
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=10)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)


@app.on_event("startup")
async def _startup() -> None:
    global _summary_task_started
    if not _summary_task_started:
        asyncio.create_task(_daily_summary_cron())
        _summary_task_started = True


@app.get("/api/summaries")
def api_summaries_list():
    if not SUMMARIES_DIR.exists():
        return []
    items = []
    for p in sorted(SUMMARIES_DIR.glob("*.md"), reverse=True)[:30]:
        items.append({"date": p.stem, "size": p.stat().st_size})
    return items


@app.get("/api/summaries/{date}")
def api_summaries_get(date: str):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if SUMMARIES_DIR.exists():
        p = SUMMARIES_DIR / f"{date}.md"
        if p.exists():
            return Response(p.read_text(), media_type="text/markdown")
    # Generate on-demand
    md = _generate_daily_summary(date)
    return Response(md, media_type="text/markdown")


# ---------------------------------------------------------------------------
# Feature 1: ntfy push notifications — triggered on new HIT events via SSE watcher
# ---------------------------------------------------------------------------
_ntfy_last_push: dict[str, float] = {}


async def _ntfy_watcher() -> None:
    """Watch the SSE event stream and push ntfy on HITs."""
    while not EVENTS_PATH.exists():
        await asyncio.sleep(2)
    inode, pos = _file_inode_size(EVENTS_PATH)
    f = open(EVENTS_PATH, "r")
    f.seek(0, os.SEEK_END)
    pos = f.tell()
    try:
        while True:
            line = f.readline()
            if line:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("hit"):
                    await _push_ntfy(ev)
                continue
            new_inode, new_size = _file_inode_size(EVENTS_PATH)
            if new_inode != inode or new_size < pos:
                f.close()
                f = open(EVENTS_PATH, "r")
                inode, pos = new_inode, 0
                continue
            pos = f.tell()
            await asyncio.sleep(0.5)
    finally:
        f.close()


async def _push_ntfy(ev: dict) -> None:
    cfg = _load_toml()
    ntfy = cfg.get("ntfy", {})
    if not ntfy.get("enabled") or not ntfy.get("topic", "").strip():
        return
    eid = ev.get("id", "")
    if _ntfy_last_push.get(eid):
        return
    _ntfy_last_push[eid] = time.time()

    topic = ntfy["topic"].strip()
    body  = (ev.get("text") or ev.get("excerpt") or "")[:500]
    kw    = ev.get("keyword") or "match"
    prio  = str(ntfy.get("priority", 4))

    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode(),
            headers={
                "Title": f"ScanRelay: {kw}",
                "Priority": prio,
                "Tags": "radio,satellite_antenna",
            },
            method="POST",
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
    except Exception:
        pass


_ntfy_task_started = False


@app.on_event("startup")
async def _startup_ntfy() -> None:
    global _ntfy_task_started
    if not _ntfy_task_started:
        asyncio.create_task(_ntfy_watcher())
        _ntfy_task_started = True


@app.post("/api/ntfy/test")
def api_ntfy_test():
    """Send a test ntfy push."""
    cfg = _load_toml()
    ntfy = cfg.get("ntfy", {})
    if not ntfy.get("topic", "").strip():
        raise HTTPException(status_code=400, detail="ntfy.topic not configured")
    topic = ntfy["topic"].strip()
    prio = str(ntfy.get("priority", 4))
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data="ScanRelay test push - if you see this it's working!".encode(),
            headers={
                "Title": "ScanRelay: test push",
                "Priority": prio,
                "Tags": "radio,white_check_mark",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ntfy push failed: {e}")


# ---------------------------------------------------------------------------
# Feature 2: Map / Geocoding
# ---------------------------------------------------------------------------

_geocache: dict[str, Any] | None = None
_geocache_mtime: float = 0.0


def _load_geocache() -> dict:
    global _geocache, _geocache_mtime
    if GEOCACHE_PATH.exists():
        mt = GEOCACHE_PATH.stat().st_mtime
        if _geocache is None or mt != _geocache_mtime:
            try:
                _geocache = json.loads(GEOCACHE_PATH.read_text())
                _geocache_mtime = mt
            except Exception:
                _geocache = {}
    elif _geocache is None:
        _geocache = {}
    return _geocache


def _save_geocache(cache: dict) -> None:
    try:
        GEOCACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GEOCACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _extract_addresses(text: str) -> list[str]:
    """Extract '<number> <street>' patterns from transcript."""
    pattern = re.compile(
        r'\b(\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*'
        r'(?:\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|'
        r'Boulevard|Blvd|Court|Ct|Way|Place|Pl|Highway|Hwy))?)\b',
        re.MULTILINE,
    )
    return list(dict.fromkeys(m.group(1) for m in pattern.finditer(text)))


@app.get("/api/map/markers")
def api_map_markers():
    """Return geocoded markers for HITs in the last 24h."""
    cutoff = time.time() - 86400
    events = _read_events(limit=5000, hits_only=True, since=cutoff)

    # Load config for center defaults
    cfg = _load_toml()
    map_cfg = cfg.get("map", {})
    center = {
        "lat":  map_cfg.get("default_lat",  39.5),
        "lon":  map_cfg.get("default_lon",  -98.35),
        "zoom": map_cfg.get("default_zoom", 4),
    }

    cache = _load_geocache()
    markers = []
    needs_geocode: list[tuple[str, dict]] = []

    for ev in events:
        text = ev.get("text", "")
        addrs = _extract_addresses(text)
        for addr in addrs[:1]:  # limit to first address per event
            if addr in cache:
                loc = cache[addr]
                if loc:
                    markers.append({
                        "lat": loc["lat"],
                        "lon": loc["lon"],
                        "text": text,
                        "category": ev.get("category", "UNKNOWN"),
                        "hit": True,
                        "ts": ev.get("ts"),
                        "id": ev.get("id"),
                        "addr": addr,
                    })
            else:
                needs_geocode.append((addr, ev))

    # Geocode missing addresses (max 5 per call, rate-limited)
    for addr, ev in needs_geocode[:5]:
        try:
            import urllib.request, urllib.parse
            url = ("https://nominatim.openstreetmap.org/search?"
                   + urllib.parse.urlencode({"q": addr, "format": "json", "limit": 1}))
            req = urllib.request.Request(url, headers={"User-Agent": "ScanRelay/3.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            if data:
                loc = {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"])}
                cache[addr] = loc
                markers.append({
                    "lat": loc["lat"], "lon": loc["lon"],
                    "text": ev.get("text", ""),
                    "category": ev.get("category", "UNKNOWN"),
                    "hit": True, "ts": ev.get("ts"),
                    "id": ev.get("id"), "addr": addr,
                })
            else:
                cache[addr] = None
            time.sleep(1.1)  # Nominatim rate limit
        except Exception:
            cache[addr] = None

    if needs_geocode:
        _save_geocache(cache)

    return {"markers": markers, "center": center}
