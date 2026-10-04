#!/usr/bin/env bash
# Online, consistent SQLite backup (safe while the service runs). Keeps 14 copies.
set -euo pipefail
APP_DIR="${APP_DIR:-/home/ubuntu/psygridevents}"
DB="$APP_DIR/data/psygridevents.sqlite3"
OUT="$APP_DIR/data/backups/psygridevents-$(date +%Y%m%d-%H%M).sqlite3"
"$APP_DIR/.venv/bin/python" - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
ls -1t "$APP_DIR"/data/backups/psygridevents-*.sqlite3 | tail -n +15 | xargs -r rm -f
echo "backup written: $OUT"
