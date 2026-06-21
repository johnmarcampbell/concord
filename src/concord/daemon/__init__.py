"""The unsupervised scraping daemon (ADR 0026).

A thin supervisor that, on a daily Tick, drives the existing Concord CLI as
child processes to keep the derived store fresh (forward incremental) and to
fill in history over time (chunked auto-backfill). It owns *scheduling* and
*backfill state*; it does no scraping itself and imports nothing from
``concord.scraper`` / ``concord.pipeline`` — it talks only to the CLI
contract (ADR 0014).

Module layout (each a thin helper, not a base class, per ADR 0007):

- :mod:`concord.daemon.state`  — the ``daemon_state.json`` watermark.
- :mod:`concord.daemon.plan`   — pure: build one Tick's job list from config + state.
- :mod:`concord.daemon.runner` — execute a :class:`~concord.daemon.plan.Job` as a subprocess.
- :mod:`concord.daemon.loop`   — the Tick loop, cadence, and signal-driven stop.
"""
