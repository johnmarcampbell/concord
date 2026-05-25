# Deploying the Concord demo on a Hostinger VPS

Target: a single Hostinger KVM1 (4 GB) running the Stage 0–2 pipeline as
cron jobs and the web layer as a long-lived systemd service. TLS is
terminated by Hostinger upstream — the app speaks plain HTTP on a
loopback port.

## One-time setup

1. **Install Python 3.12 and uv** on the VPS (per Hostinger's standard
   instructions for Python apps).
2. **Clone the repo** to `/opt/concord` and install the package:
   ```sh
   git clone https://github.com/johnmarcampbell/concord /opt/concord
   cd /opt/concord
   uv sync --frozen
   ```
3. **Create a dedicated user** to run the service:
   ```sh
   useradd -r -m -d /var/lib/concord -s /usr/sbin/nologin concord
   chown -R concord:concord /opt/concord /var/lib/concord
   ```
4. **Place API keys** in a file readable only by the `concord` user:
   ```sh
   cat > /etc/concord.env <<'EOF'
   CONGRESS_API_KEY=…
   OPENAI_API_KEY=…
   EOF
   chmod 600 /etc/concord.env
   chown concord:concord /etc/concord.env
   ```
5. **Set an OpenAI account-level spend cap** at
   <https://platform.openai.com/account/limits>. Belt-and-suspenders for
   the per-IP rate limit on `/search`. A $5–$10/month cap is more than
   enough for the demo's expected traffic.

## Initial backfill

Optional: do the heavy initial pull + index on your dev machine, then
`rsync` the resulting SQLite + JSONL to the VPS. Saves several hours of
VPS time. Otherwise, run on the VPS:

```sh
sudo -u concord -E /opt/concord/.venv/bin/concord pull \
    --from 2021-01-01 --to 2026-05-22 \
    --storage /var/lib/concord/proceedings.jsonl
sudo -u concord -E /opt/concord/.venv/bin/concord load \
    --jsonl /var/lib/concord/proceedings.jsonl \
    --db /var/lib/concord/proceedings.db
sudo -u concord -E /opt/concord/.venv/bin/concord index \
    --db /var/lib/concord/proceedings.db
```

(Pre-loading the env file with `set -a; source /etc/concord.env; set +a`
before each command keeps the keys out of your shell history.)

## Web service

`/etc/systemd/system/concord-web.service`:

```ini
[Unit]
Description=Concord web demo
After=network.target

[Service]
Type=simple
User=concord
Group=concord
WorkingDirectory=/opt/concord
EnvironmentFile=/etc/concord.env
ExecStart=/opt/concord/.venv/bin/concord serve \
    --db /var/lib/concord/proceedings.db \
    --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```sh
systemctl daemon-reload
systemctl enable --now concord-web
systemctl status concord-web
```

## Hostinger reverse proxy

In the Hostinger dashboard, route the public `your-domain.com` (or chosen
subdomain) to `127.0.0.1:8000`. TLS terminates at the Hostinger edge; the
app sees plain HTTP. No additional certificate management needed.

## Daily updates via cron

`/etc/cron.d/concord` (running as the `concord` user):

```cron
# At 04:30 UTC every day: pull yesterday's record, load it, index it.
30 4 * * * concord set -a; . /etc/concord.env; set +a; \
    /opt/concord/.venv/bin/concord pull \
      --from "$(date -u -d yesterday +\%Y-\%m-\%d)" \
      --to "$(date -u -d yesterday +\%Y-\%m-\%d)" \
      --storage /var/lib/concord/proceedings.jsonl >> /var/log/concord-pull.log 2>&1
40 4 * * * concord /opt/concord/.venv/bin/concord load \
      --jsonl /var/lib/concord/proceedings.jsonl \
      --db /var/lib/concord/proceedings.db >> /var/log/concord-load.log 2>&1
45 4 * * * concord set -a; . /etc/concord.env; set +a; \
    /opt/concord/.venv/bin/concord index \
      --db /var/lib/concord/proceedings.db >> /var/log/concord-index.log 2>&1
```

The running `concord-web` service picks up the new rows automatically:
WAL mode + fresh-connection-per-request means each `/search` and
`/proceedings/{id}` request sees the latest committed state without a
restart.

## Backups

JSONL is the canonical raw store (per ADR-0002). The SQLite database is
regenerable from it.

- **`proceedings.jsonl`** — back this up. It's append-only, so a daily
  rsync to off-box storage is sufficient.
- **`proceedings.db`** — optional. A backup just saves you the ~hours of
  re-indexing time if the disk dies. `sqlite3 proceedings.db ".backup
  /backup/proceedings.db"` is the safe way to copy it while the web
  service is running.

## Troubleshooting

- **Service won't start**: `journalctl -u concord-web -n 100`. Most
  common cause: a missing API key in `/etc/concord.env`.
- **Search returns 429**: someone hit the per-IP rate limit. Reset by
  waiting 60 seconds, or temporarily widen the cap via the
  `SEARCH_RATE_LIMIT` constant in `src/concord/web/app.py`.
- **`sqlite3.OperationalError: no such module: vec0`**: the
  `sqlite-vec` extension didn't load. Verify with
  `/opt/concord/.venv/bin/python -c "import sqlite_vec; print(sqlite_vec.loadable_path())"`.
