#!/usr/bin/env bash
#
# ScanRelay installer for Raspberry Pi OS Bookworm (Lite or full).
# Run as root: sudo bash install.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

PROJECT_DIR="/opt/scanrelay"
VENV_DIR="${PROJECT_DIR}/venv"
WHISPER_DIR="/opt/whisper.cpp"
DATA_DIR="/var/lib/scanrelay"
CONFIG_DIR="/etc/scanrelay"
SERVICE_USER="scanrelay"

# Pick the smallest English-only quantized model that handles addresses reliably.
WHISPER_MODEL="base.en-q5_1"

# ---------------------------------------------------------------------------
log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m   %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m  %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."

log "Installing apt packages"
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    alsa-utils \
    build-essential cmake git \
    libsdl2-dev \
    ca-certificates

# ---------------------------------------------------------------------------
log "Creating service user '${SERVICE_USER}'"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --home "${PROJECT_DIR}" --shell /usr/sbin/nologin \
            --groups audio,dialout "${SERVICE_USER}"
else
    # Make sure they're in the right groups (audio for ALSA, dialout for serial).
    usermod -aG audio,dialout "${SERVICE_USER}"
fi

# ---------------------------------------------------------------------------
log "Creating directories"
mkdir -p "${PROJECT_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/tmp" "${DATA_DIR}/audio" "${CONFIG_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${DATA_DIR}"

# ---------------------------------------------------------------------------
log "Copying ScanRelay source to ${PROJECT_DIR}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cp -r "${SRC_DIR}/scanrelay" "${PROJECT_DIR}/"
cp -r "${SRC_DIR}/dashboard" "${PROJECT_DIR}/"
[[ -f "${SRC_DIR}/scanrelay.toml.example" ]] && \
    cp "${SRC_DIR}/scanrelay.toml.example" "${CONFIG_DIR}/"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${PROJECT_DIR}"

if [[ ! -f "${CONFIG_DIR}/scanrelay.toml" ]]; then
    log "Seeding ${CONFIG_DIR}/scanrelay.toml from example"
    cp "${CONFIG_DIR}/scanrelay.toml.example" "${CONFIG_DIR}/scanrelay.toml"
fi

# ---------------------------------------------------------------------------
log "Creating Python venv"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip wheel
"${VENV_DIR}/bin/pip" install \
    webrtcvad-wheels \
    meshtastic \
    pyserial \
    fastapi \
    'uvicorn[standard]' \
    pydantic
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${VENV_DIR}"

# ---------------------------------------------------------------------------
log "Building whisper.cpp (this takes 5-10 minutes on Pi 5)"
if [[ ! -d "${WHISPER_DIR}" ]]; then
    git clone --depth 1 https://github.com/ggerganov/whisper.cpp "${WHISPER_DIR}"
fi
(
    cd "${WHISPER_DIR}"
    git pull --ff-only || true
    # Recent whisper.cpp uses CMake and produces ./build/bin/whisper-cli
    cmake -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build --config Release -j"$(nproc)"
)

log "Downloading whisper model: ${WHISPER_MODEL}"
(
    cd "${WHISPER_DIR}"
    bash ./models/download-ggml-model.sh "${WHISPER_MODEL}"
)

# ---------------------------------------------------------------------------
log "Installing systemd units"
cp "${SRC_DIR}/systemd/scanrelay.service" /etc/systemd/system/
if [[ -f "${SRC_DIR}/systemd/scanrelay-dashboard.service" ]]; then
    cp "${SRC_DIR}/systemd/scanrelay-dashboard.service" /etc/systemd/system/
fi
systemctl daemon-reload

cat <<EOF

==============================================================================
ScanRelay installed.

Next steps:

  1. Plug in the USB sound card and check it's detected:
        arecord -l

  2. Edit /etc/scanrelay/scanrelay.toml — confirm the ALSA device name
     matches what arecord shows (it's the most common thing to get wrong).

  3. Test capture for 5 seconds to make sure audio is flowing:
        sudo -u ${SERVICE_USER} arecord -D plughw:CARD=Device,DEV=0 \\
            -f S16_LE -c 1 -r 16000 -d 5 /tmp/test.wav
        aplay /tmp/test.wav

  4. Test whisper.cpp on that recording:
        ${WHISPER_DIR}/build/bin/whisper-cli \\
            -m ${WHISPER_DIR}/models/ggml-${WHISPER_MODEL}.bin \\
            -f /tmp/test.wav -t 3

  5. Start the daemon:
        sudo systemctl enable --now scanrelay
        sudo journalctl -u scanrelay -f

  6. Watch the event log:
        tail -f /var/lib/scanrelay/logs/events.jsonl

  7. Start the web dashboard (optional):
        sudo systemctl enable --now scanrelay-dashboard
        Then open http://<pi-ip>:8080 from your phone or laptop.

==============================================================================
EOF
