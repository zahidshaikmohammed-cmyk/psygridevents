# Deploying PSYGRIDEVENTS on Oracle Always Free

The service runs next to PSYGRID on the same VM (`140.245.226.102`). It reads PSYGRID over
loopback, needs about 100–300 MB of RAM and no GPU, and needs no paid service.

## 0. Prerequisites

* Ubuntu (22.04 or 24.04) VM with PSYGRID running on port 10000 (`curl -s localhost:10000/health`).
* Python 3.11 or newer.
  * Ubuntu 24.04 ships 3.12.
  * On 22.04: `sudo apt-get install -y python3.11 python3.11-venv`, then prefix the install command with `PYTHON=python3.11`.
* The `ubuntu` user with sudo (the Oracle default).
* Outbound HTTPS from the VM (the default security list allows egress).

## 1. First-time install

```bash
ssh ubuntu@140.245.226.102
git clone https://github.com/zahidshaikmohammed-cmyk/psygridevents.git ~/psygridevents   # skip if CI already cloned it
cd ~/psygridevents
git checkout main && git pull
bash deploy/install_oracle.sh
```

The installer is idempotent. It:

1. creates `.venv` and runs `pip install -e .`;
2. creates `data/`;
3. creates `/etc/psygridevents.env` from `.env.example`, mode 600, if it doesn't exist;
4. installs and enables `deploy/psygridevents.service` and restarts it;
5. installs and enables the `psygridevents-backup.timer` systemd timer (weekdays 16:45 IST, runs `deploy/backup_db.sh`);
6. waits up to 60 s for `/health` and prints the service log if it never answers.

## 2. Configure

```bash
sudo nano /etc/psygridevents.env
```

The minimum production setting is `PSYGRID_BASE_URL=http://127.0.0.1:10000`. Everything else
has working defaults (see `.env.example` and `config/runtime.yaml`). Optional settings:

* `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for Telegram delivery.
* `PSYGRIDEVENTS_API_TOKEN`, if you expose the API.
* `PSYGRIDEVENTS_AI_ENABLED=1` with `ANTHROPIC_API_KEY`, plus `.venv/bin/pip install -e ".[ai]"`, for the AI assist.

Add the year's NSE holidays to `config/market_calendar.yaml` from the official NSE circular.

Then:

```bash
sudo systemctl restart psygridevents
```

## 3. Verify from the VM

```bash
curl -s localhost:10100/health
curl -s localhost:10100/market/health | python3 -m json.tool | head -40
curl -s localhost:10100/providers | python3 -c 'import json,sys;[print(p["provider_id"],p["state"],p["last_error"]) for p in json.load(sys.stdin)["providers"]]'
.venv/bin/python -m psygridevents.main --probe-sources | head -80   # one-shot probe of every source from this IP
.venv/bin/python -m psygridevents.main --refresh-issuers             # official NSE names/ISINs into the DB
journalctl -u psygridevents -f                                       # live logs (JSON lines)
```

## 4. Live signals

| What | Command |
|---|---|
| Machine-readable | `curl -s localhost:10100/signals/top` |
| Human-readable | `curl -s 'localhost:10100/signals/top?format=text'` |
| Dashboard on your laptop (no port exposed) | `ssh -L 10100:127.0.0.1:10100 ubuntu@140.245.226.102`, then open http://localhost:10100/ |
| Signal alerts as they happen | `journalctl -u psygridevents -f \| grep "SIGNAL ALERT"` |

To expose the API publicly instead:

1. Set `PSYGRIDEVENTS_API_HOST=0.0.0.0` and a long random `PSYGRIDEVENTS_API_TOKEN`.
2. Open TCP 10100 in the VCN security list and in `iptables`.
3. Use `http://140.245.226.102:10100/signals/top?token=...`.

## 5. Updates

Pushing to `main` runs the tests and then the existing CI deploy job (`.github/workflows/test.yml`).
That job SSHes into the VM, resets the checkout to `origin/main` and runs `deploy/install_oracle.sh`,
which restarts the service. State survives because SQLite and `/etc/psygridevents.env` are never
touched.

To update by hand: `cd ~/psygridevents && git pull && bash deploy/install_oracle.sh`.

## 6. Operations

| Task | Command |
|---|---|
| Stop or start | `sudo systemctl stop psygridevents` / `start` (SIGTERM gives a graceful shutdown) |
| Backups | `data/backups/psygridevents-YYYYMMDD-HHMM.sqlite3` (14 kept) |
| Restore | `sudo systemctl stop psygridevents && cp data/backups/<file> data/psygridevents.sqlite3 && sudo systemctl start psygridevents` |
| Resync the PSYGRID universe after Psygrid changes `stocks.json` | `.venv/bin/python tools/sync_universe_from_psygrid.py --psygrid-url https://raw.githubusercontent.com/zahidshaikmohammed-cmyk/Psygrid/main/stocks.json`, then commit `config/instruments.json` |
| Disk usage | About 20–60 MB per month of SQLite; raw payloads are pruned after 45 days |
| Resource limits | `MemoryHigh=500M`/`MemoryMax=700M`, `CPUWeight=20`, `CPUQuota=80%`, idle I/O, `OOMScoreAdjust=900`, `TasksMax=128` in the unit file. PSYGRID always wins: under pressure the kernel kills this service, never PSYGRID |
| Isolation from PSYGRID | Reads PSYGRID over HTTP only. It never starts, stops or restarts `psygrid`, and `ProtectHome=read-only` with `ReadWritePaths` limited to its own checkout means it cannot write PSYGRID's files. With no live PSYGRID data it produces no market signals |
