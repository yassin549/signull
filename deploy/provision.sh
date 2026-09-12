#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/signull"
SVC_USER="signull"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

if [ ! -f "$APP_DIR/main.py" ]; then
  echo "Missing $APP_DIR/main.py - extract the deploy archive first." >&2
  exit 1
fi

# --- swap (small box has none; pip/torch can OOM without it) ---
if ! swapon --show | grep -q .; then
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-dev build-essential

if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi

PIP="$APP_DIR/.venv/bin/pip"
"$PIP" install --upgrade pip wheel

# CPU-only torch: much smaller than the default CUDA wheels, which this box cannot use.
# Fall back to the default index if no CPU wheel exists for this Python version.
"$PIP" install torch --index-url https://download.pytorch.org/whl/cpu || "$PIP" install torch

"$PIP" install -r "$APP_DIR/requirements.txt"

chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

install -m 0644 "$APP_DIR/deploy/signull.service" /etc/systemd/system/signull.service
systemctl daemon-reload
systemctl enable signull
systemctl restart signull
sleep 3
systemctl --no-pager --full status signull || true
