# syntax=docker/dockerfile:1.7
#
# Concord container image.
#
# A generic, deployment-agnostic image. The default command runs the web
# server, but the image exposes the full `concord` CLI — override the
# command to run pipeline subcommands (`pull`, `load`, `index`, ...).
#
# State lives under /app/data, which is meant to be a bind mount or
# named volume. See docs/docker.md for usage.

# ---------------------------------------------------------------------------
# Builder: resolve and install dependencies with uv into /app/.venv.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install third-party dependencies first, before copying any source.
# This layer is cached as long as pyproject.toml + uv.lock are unchanged,
# so day-to-day edits to src/ don't re-run dependency resolution.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now install the project itself. Fast — deps are already in place.
COPY src/ src/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime: slim Python image carrying just the venv, the source, and a
# small set of debug niceties.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# - ca-certificates: HTTPS to api.congress.gov + OpenAI.
# - curl: used by the HEALTHCHECK and handy for in-container debugging.
# - htop, sqlite3, less: debug conveniences. The whole project's state
#   is one SQLite file — `sqlite3 /app/data/proceedings.db` is the
#   fastest way to inspect FTS5 / vec / row counts when something is off.
# - tmux, neovim: in-container editing/session conveniences for poking
#   around live.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        htop \
        less \
        neovim \
        sqlite3 \
        tmux \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with a fixed UID/GID so host bind mounts have a stable
# ownership target. See docs/docker.md for the one-time `chown` step
# host-side machines need when their login user is not UID 1000.
# `--system` would force a UID < 1000, fighting the explicit `--uid 1000`
# (UID 1000 is what we want for bind-mount UID matching). So this is a
# regular user account that simply never logs in (nologin shell).
RUN groupadd --gid 1000 concord \
    && useradd  --uid 1000 --gid 1000 --no-create-home \
                --home-dir /app --shell /sbin/nologin concord

WORKDIR /app

COPY --from=builder --chown=concord:concord /app/.venv          /app/.venv
COPY --from=builder --chown=concord:concord /app/src            /app/src
COPY --from=builder --chown=concord:concord /app/pyproject.toml /app/pyproject.toml
COPY --from=builder --chown=concord:concord /app/README.md      /app/README.md

# /app/data is the convention for the mounted data volume. Create it so
# the container can boot even without a mount — the web app's first-boot
# bootstrap (ADR 0012) will create an empty proceedings.db on demand.
RUN mkdir -p /app/data && chown concord:concord /app/data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER concord

EXPOSE 8000

# Hit the dedicated /healthz endpoint rather than /, which would render
# Jinja templates and do real work on every probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# Default: serve the web app on 0.0.0.0:8000. Override CMD to run any
# other `concord` subcommand (pull, load, index, ...).
# Note: the CLI default is --host 127.0.0.1, which is wrong inside a
# container — we override it here, not in the CLI itself.
CMD ["concord", "serve", "--host", "0.0.0.0", "--port", "8000"]
