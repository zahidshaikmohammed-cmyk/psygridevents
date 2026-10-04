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

# Daily SQLite backup at 16:45 IST (11:15 UTC), keeping 14 days.
CRON_LINE="15 11 * * 1-5 $APP_DIR/deploy/backup_db.sh >> $APP_DIR/data/backups/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v backup_db.sh; echo "$CRON_LINE" ) | crontab -

sleep 8
curl -fsS "http://127.0.0.1:${PSYGRIDEVENTS_API_PORT:-10100}/health" && echo && echo "psygridevents is running."
