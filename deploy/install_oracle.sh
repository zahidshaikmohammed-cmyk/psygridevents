#!/usr/bin/env bash
# Idempotent install/upgrade of PSYGRIDEVENTS on an Oracle Always Free Ubuntu VM.
# Usage (as the ubuntu user, from the repo checkout):  bash deploy/install_oracle.sh
set -euo pipefail
APP_DIR="${APP_DIR:-/home/ubuntu/psygridevents}"
cd "$APP_DIR"

if ! command -v python3 >/dev/null; then sudo apt-get update && sudo apt-get install -y python3 python3-venv; fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"' || {
  echo "Python 3.11+ is required (Ubuntu 22.04: sudo apt-get install -y python3.11 python3.11-venv; PYTHON=python3.11)"; exit 1; }
PYTHON="${PYTHON:-python3}"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e . >/dev/null
mkdir -p data data/backups

if [ ! -f /etc/psygridevents.env ]; then
  sudo install -m 600 -o ubuntu -g ubuntu .env.example /etc/psygridevents.env
  echo "Created /etc/psygridevents.env from .env.example -- review it."
fi
sudo install -m 644 deploy/psygridevents.service /etc/systemd/system/psygridevents.service
sudo systemctl daemon-reload
sudo systemctl enable psygridevents >/dev/null
sudo systemctl restart psygridevents

# Daily SQLite backup on weekdays at 16:45 IST via a systemd timer (no cron dependency).
sudo install -m 644 deploy/psygridevents-backup.service /etc/systemd/system/psygridevents-backup.service
sudo install -m 644 deploy/psygridevents-backup.timer /etc/systemd/system/psygridevents-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now psygridevents-backup.timer >/dev/null

# Wait for the API to answer (the first start restores state and probes sources).
PORT="${PSYGRIDEVENTS_API_PORT:-10100}"
for attempt in $(seq 1 30); do
  if .venv/bin/python - "$PORT" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=3) as r:
        print(json.dumps(json.load(r)))
except Exception:
    sys.exit(1)
PY
  then
    echo "psygridevents is running."
    exit 0
  fi
  sleep 2
done
echo "psygridevents did not answer /health within 60s"
sudo systemctl status psygridevents --no-pager || true
sudo journalctl -u psygridevents -n 80 --no-pager || true
exit 1
