# Running Concord in Docker

The repository ships a generic Dockerfile. The image exposes the full
`concord` CLI; the default command runs the web server, and any other
subcommand can be invoked by overriding the command at `docker run`.

The image is deliberately deployment-agnostic — no Hostinger-specific
or reverse-proxy logic lives in it. Wire up environment variables,
volumes, ports, and TLS in your `docker-compose.yml` (or equivalent)
where the image is actually deployed.

## Build

```sh
docker build -t concord .
```

## Run the web server

```sh
docker run --rm \
    -p 8000:8000 \
    -v "$(pwd)/data:/app/data" \
    -e OPENAI_API_KEY=sk-... \
    concord
```

The server binds `0.0.0.0:8000` inside the container; map it to
whatever host port you like with `-p`. State lives under `/app/data`
(SQLite `proceedings.db`, the canonical JSONL stores, and any
entity-specific stores under that directory).

On first start against an empty `/app/data`, the web app bootstraps an
empty SQLite schema (per [ADR 0012](adr/0012-web-bootstraps-empty-schema-on-startup.md)).
The UI loads, but search and entity pages return nothing until you
ingest data.

## Run a pipeline command

The image is a generic CLI image — override the command:

```sh
docker run --rm \
    -v "$(pwd)/data:/app/data" \
    -e CONGRESS_API_KEY=... \
    -e OPENAI_API_KEY=sk-... \
    concord \
    concord pull --from 2026-05-01 --to 2026-05-22
```

`concord load`, `concord index`, and the entity-specific pipelines
(`concord pull-members`, `concord pull-bills`, ...) all follow the
same pattern. Pass `concord --help` as the command to see the full
list.

## Host directory ownership

The container runs as a non-root user with fixed UID/GID **`1000:1000`**.
For host bind mounts to be writable by the container:

```sh
mkdir -p ./data
sudo chown -R 1000:1000 ./data
```

No host user needs to *exist* with UID 1000 — the kernel only checks
the number.

Two footnotes:

- **If your host login user is already UID 1000** (common on
  single-admin Linux boxes — check with `id`), skip the `chown`. Files
  written by the container will show up as owned by your login user on
  the host, which is convenient for `ls`.
- **If your host login user is *not* UID 1000**, the `chown` works
  fine, but `ls -l ./data` on the host shows files owned by a numeric
  UID with no name. Cosmetic, not functional.

macOS users running Docker Desktop don't need to think about any of
this — Docker Desktop's file-sharing layer handles UID translation.

## Required environment variables

- **`OPENAI_API_KEY`** — required by `concord serve` (semantic
  search) and `concord index` (embedding generation).
- **`CONGRESS_API_KEY`** — required by the scraper subcommands
  (`pull`, `pull-members`, `pull-bills`, ...).

Both are read from the process environment; the image does not bundle
any defaults or fallbacks.

## Compose

A minimal `docker-compose.yml` for a single-host deployment:

```yaml
services:
  concord:
    build: .                       # or: image: ghcr.io/<you>/concord:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"      # behind a reverse proxy
    volumes:
      - ./data:/app/data
    environment:
      - OPENAI_API_KEY
      - CONGRESS_API_KEY
    env_file:
      - .env                       # not committed
```

Pipeline jobs run as one-shot containers off the same image:

```sh
docker compose run --rm concord concord pull --from "$(date -u +%F)" --to "$(date -u +%F)"
docker compose run --rm concord concord load
docker compose run --rm concord concord index
```

Schedule those via host-side `cron` (or `systemd` timers) — the image
itself stays single-purpose.

## Image internals

- **Base:** `python:3.12-slim` (Debian bookworm, glibc — required by
  the `sqlite_vec` wheel and by stdlib `sqlite3`'s loadable-extension
  support).
- **Build:** multi-stage via the official `ghcr.io/astral-sh/uv`
  builder image. Runtime carries only `/app/.venv` and the source tree
  — no `uv`, no build toolchain.
- **Debug tools baked in:** `htop`, `sqlite3`, `curl`, `less`. Useful
  for poking at a running container with `docker compose exec`.
- **Healthcheck:** `curl -fsS http://localhost:8000/healthz`.
- **User:** non-root, UID/GID `1000:1000`.
