# ScanRelay

Listens to the Uniden BCD436HP scanner, transcribes each transmission with `whisper.cpp` on the Pi 5, and relays any transmission that mentions **My Keyword** or the road **12345** over a private Meshtastic channel (index 2) to your Meshtastic nodes.

Everything else is dropped silently.

## Pipeline

```
USB sound card → arecord → webrtcvad → whisper.cpp (base.en-q5_1)
                                                 │
                                                 ▼
                            keyword filter: "my keyword" OR /\b12345\b/ etc.
                                                 │ (hit)
                                                 ▼
                              [SITE 22:53] <sentence with the hit>
                                                 │
                                                 ▼
                                  Meshtastic ch.1 → Heltec V4 mesh
```

## Hardware assumed

- Raspberry Pi 5 (4 GB+), active cooler
- RAK6421 WisHat + RAK13302 1W LoRa booster in slot 2
- USB sound card (any class-compliant; CM108-based works well)
- BCD436HP scanner audio out → USB sound card line in
- meshtasticd already running on localhost:4403 with a private channel at index 1 named `Scanner`

## Install

```bash
git clone <this repo> /tmp/scanrelay     # or scp the folder over
cd /tmp/scanrelay
sudo bash scripts/install.sh
```

The installer:

1. Installs apt deps (alsa-utils, build tools)
2. Creates a `scanrelay` system user (in groups `audio`, `dialout`)
3. Copies the package to `/opt/scanrelay`
4. Builds a Python venv with `webrtcvad-wheels`, `meshtastic`, `pyserial`
5. Clones and builds `whisper.cpp` in `/opt/whisper.cpp`
6. Downloads the `base.en-q5_1` quantized model (~58 MB)
7. Installs the systemd unit

## Configure

Edit `/etc/scanrelay/scanrelay.toml`. The one thing you'll almost certainly need to change is the ALSA device name:

```bash
arecord -l
```

Look for your USB sound card, then set `audio.device` to the matching `plughw:CARD=...,DEV=0` form.

## Run

```bash
sudo systemctl enable --now scanrelay
sudo journalctl -u scanrelay -f
```

You'll see lines like:

```
[INFO] Transmission: 4.20s
[INFO] Transcript (4.20s audio / 1.85s compute): Engine 7 responding to a grass fire near 12345 Lake Road
[INFO] Sent (62 bytes): [SITE 22:53] Engine 7 responding to a grass fire near 12345 Lake Road
```

Every transmission (hit or not) is also appended to `/var/lib/scanrelay/logs/events.jsonl` so you can audit accuracy later.

## Web dashboard (optional)

There's a small FastAPI dashboard you can run on the same Pi. It gives you:

- A live feed of every transmission with HIT/MISS badges
- Per-transmission audio playback (the daemon saves a WAV per event, rotated by count + total MB)
- A "hits only" filter
- A "match all" toggle that flips `filter.match_all` in the toml and restarts the daemon, so you can drop into raw-traffic mode for a while and then flip back to keyword filtering
- Today's transmission/hit counts and daemon uptime
- Mobile-friendly dark UI

Start it:

```bash
sudo systemctl enable --now scanrelay-dashboard
```

Then open `http://<pi-ip>:8080` from your phone or laptop. It binds to `0.0.0.0:8080` by default — change `--host 0.0.0.0` to `127.0.0.1` in `/etc/systemd/system/scanrelay-dashboard.service` if you'd rather access it only via SSH tunnel.

Audio retention is capped in `scanrelay.toml` under `[dashboard]` (`audio_max_files` and `audio_max_mb`).

## What gets relayed

Two filters, OR'd together:

1. **Substring keywords** (default): `my keyword`, `my kw`, `mykeyword`, `my-keyword`
2. **Regex patterns** for "12345" specifically:
   - `\b12345\b` — digit form, but only as a standalone number (won't match `123450` or `312345`)
   - `\btwelve[\s-]+oh[\s-]+one\b` — Whisper often writes road numbers as words
   - `\btwelve[\s-]+hundred(?:[\s-]+and)?[\s-]+one\b`
   - `\bone[\s-]+thousand[\s-]+two[\s-]+hundred(?:[\s-]+and)?[\s-]+one\b`

Tested against 19 real and adversarial cases (`tests/test_filter.py`) — all pass.

## Alert format

```
[SITE HH:MM] <sentence(s) from the transcript containing the hit>
```

- Site tag (`SITE` by default) so other nodes know who originated it
- 24-hour local time of when the transmission *started* on the radio
- The actual sentence(s) — usually the dispatcher's full callout

Capped at ~200 bytes UTF-8 to fit one Meshtastic text packet on LongFast.

## Tuning

- **Too many false alerts:** raise `vad.aggressiveness` to 3, or raise `vad.min_event_seconds`.
- **Missing short callouts:** lower `vad.min_event_seconds` (but expect more squelch-tail garbage).
- **Whisper too slow on Pi 5:** drop to `tiny.en-q5_1` (much faster, less accurate on addresses).
- **Different keywords/roads:** edit `filter.keywords` and `filter.keyword_patterns` in the toml.

## Debugging

```bash
# What's happening right now
sudo journalctl -u scanrelay -f

# Every transmission ever heard, with transcript and hit/miss
tail -f /var/lib/scanrelay/logs/events.jsonl | jq

# Test the filter against ad-hoc text
python3 -c "from scanrelay.config import FilterConfig; from scanrelay.keyword_filter import Filter; \
  print(Filter(FilterConfig()).find_hit('your test sentence here'))"

# Test whisper directly on a captured event
ls /var/lib/scanrelay/tmp/  # nothing here normally — files are deleted after use
```

## Files

```
scanrelay/
├── scanrelay/
│   ├── __init__.py
│   ├── config.py           # all tunables; loads /etc/scanrelay/scanrelay.toml
│   ├── audio_capture.py    # arecord wrapper, yields PCM frames
│   ├── vad_gate.py         # webrtcvad with start/end hysteresis + pre-roll
│   ├── transcriber.py      # whisper.cpp subprocess wrapper
│   ├── keyword_filter.py   # substring + regex filter, deduper
│   ├── formatter.py        # builds the LoRa text alert
│   ├── mesh_sender.py      # Meshtastic TCP, rate-limited
│   └── daemon.py           # main loop
├── tests/
│   └── test_filter.py      # 19 cases, all passing
├── scripts/
│   └── install.sh
├── dashboard/
│   ├── server.py           # FastAPI app (events tail, SSE, audio, match_all)
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── systemd/
│   ├── scanrelay.service
│   └── scanrelay-dashboard.service
├── scanrelay.toml.example
└── README.md
```
