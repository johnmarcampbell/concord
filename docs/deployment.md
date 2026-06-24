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

The one-time historical pull is a **Backfill** (CONTEXT.md → Orchestration):
a deliberate, wide-window `concord run <entity>` over old dates and closed
Congresses. It is distinct from the recurring **Sync** below, which only ever
covers a bounded window. Run each entity's full pipeline (scrape → load →
index) once; everything is idempotent, so a re-run is a no-op.

Optional: do the heavy backfill on your dev machine, then `rsync` the
resulting SQLite + JSONL to the VPS. Saves several hours of VPS time.
Otherwise, run on the VPS (all stores under `/var/lib/concord`):

```sh
sudo -u concord -E /opt/concord/.venv/bin/concord run proceedings \
    --from 2021-01-01 --to 2026-05-22 \
    --storage /var/lib/concord/proceedings.jsonl \
    --db /var/lib/concord/proceedings.db
sudo -u concord -E /opt/concord/.venv/bin/concord run members \
    --congresses 117,118,119 \
    --storage /var/lib/concord/members.jsonl \
    --db /var/lib/concord/proceedings.db
sudo -u concord -E /opt/concord/.venv/bin/concord run bills \
    --congresses 117,118,119 \
    --storage-dir /var/lib/concord \
    --db /var/lib/concord/proceedings.db
sudo -u concord -E /opt/concord/.venv/bin/concord run votes \
    --congresses 117,118,119 \
    --storage-dir /var/lib/concord \
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

## Daily updates via `concord sync`

A **Sync** (CONTEXT.md → Orchestration) is one bounded, best-effort
incremental pass over all four entities — scrape → load → index for each —
in a single tested command. The scheduler owns the cadence; Concord owns the
orchestration. See [ADR 0026](adr/0026-sync-command-not-resident-daemon.md)
for why this is a scheduled command and not a resident daemon.

`/etc/cron.d/concord` (running as the `concord` user):

```cron
# At 04:30 UTC every day: one Sync over all four entities.
# Proceedings use a rolling 7-day window (--lookback-days); members, bills,
# and votes cover the current Congress with skip-unchanged always on.
30 4 * * * concord set -a; . /etc/concord.env; set +a; \
    /opt/concord/.venv/bin/concord sync \
      --db /var/lib/concord/proceedings.db \
      --no-progress >> /var/log/concord-sync.log 2>&1
```

The JSONL stores and the overlap lock live alongside `--db` (so
`/var/lib/concord/`). No `flock(1)` wrapper is needed: `concord sync` takes
its own advisory `flock` and a second invocation that overlaps a still-running
Sync exits cleanly with code `75` rather than doing duplicate work.

Exit codes worth alerting on: `0` all entities ok · `1` one or more entities
failed (the others still ran — best-effort) · `2` a required API key is
missing · `75` another Sync was already running. A one-line-per-entity summary
is written to stderr (captured in `concord-sync.log`).

The running `concord-web` service picks up the new rows automatically:
WAL mode + fresh-connection-per-request means each `/search` and
`/proceedings/{id}` request sees the latest committed state without a
restart.

> Prefer `systemd` timers? A `concord-sync.timer` + oneshot
> `concord-sync.service` firing the same command is equally valid — and since
> `systemd` already supervises `concord-web` on this box, it is the lighter
> option. The `flock` guard makes either scheduler safe.

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
