# ScanRelay

ScanRelay is a Raspberry Pi daemon that listens to a scanner over USB audio, transcribes each transmission with `whisper.cpp`, matches the configured keywords, and relays hits to Meshtastic plus optional ntfy push notifications.

Default v3.2.1 keywords are **moss lake**, **moss lk**, **mosslake**, **moss-lake**, plus regex patterns for **1201** digit and spoken variants.

## v3.2.1 behavior

- Alerts fire 24/7. There is no quiet-hours suppression in ScanRelay; use iOS Focus or notification settings to quiet alerts on the phone side.
- ntfy pushes are sent by the daemon after a Meshtastic send succeeds.
- ntfy supports per-keyword priority/tags and optional MP3 audio attachments converted from the saved WAV.
- The dashboard has a Live panel backed by `/api/live` and `/ws/live`, plus a “What did I miss” catch-up widget from `/api/catch-up`.
- A silence alerter can push ntfy when no keyword hits arrive for the configured threshold.

## Pipeline

```
USB sound card → arecord → webrtcvad → whisper.cpp
                                                 │
                                                 ▼
                   keyword filter: moss lake variants OR 1201 variants
                                                 │ (hit)
                                                 ▼
                              [SITE 22:53] <sentence with the hit>
                                  ├─ Meshtastic channel
                                  └─ ntfy push (+ optional MP3 audio)
```

## Hardware assumed

- Raspberry Pi 5
- USB sound card connected to scanner audio out
- `meshtasticd` reachable on localhost TCP
- `ffmpeg` installed for ntfy MP3 attachments
- `whisper.cpp` model available locally

## Install

```bash
git clone <this repo> /tmp/scanrelay
cd /tmp/scanrelay
sudo bash scripts/install.sh
```

## Configure

Copy `scanrelay.toml.example` to `/etc/scanrelay/scanrelay.toml` and edit:

```bash
arecord -l
```

Set `[audio].device` to your USB sound card. Configure `[ntfy]` with your topic, for example `scanrelay-shea-k7m2`, and tune `[[filter.keyword_priorities]]` as needed.

## Run

```bash
sudo systemctl enable --now scanrelay
sudo journalctl -u scanrelay -f
```

Every transmission is appended to `/var/lib/scanrelay/logs/events.jsonl`; saved audio lives in `/var/lib/scanrelay/audio` when dashboard audio capture is enabled.

## Web dashboard

```bash
sudo systemctl enable --now scanrelay-dashboard
```

Open `http://<pi-ip>:8080`. The dashboard includes live audio level, latest event, event feed, hit filters, search, health, audio playback, catch-up, and configuration editing.

## Tests

```bash
python -m pytest tests/ -x
```
