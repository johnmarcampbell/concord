# 0012 — Web layer bootstraps an empty schema on startup

**Status**: Accepted, 2026-05-26.

## Context

`concord serve` historically refused to start if its `--db` path didn't exist: the CLI did a `db_path.exists()` check up front and exited with code 2 if the file wasn't there. That was fine when the only path to a running web app went through `concord scrape` → `concord load` → `concord index` on a developer's machine — the DB is created as a side effect of the pipeline, and "no DB" almost always means "you forgot to run the pipeline."

That assumption breaks the moment we ship a Dockerfile. The intended Docker first-run UX is: pull the image, `docker run`, hit `localhost:8000`, see an empty Concord with the search box and "no results yet" copy, then optionally exec into the container to run a scrape. With the old behavior, the container crash-loops on first boot with `error: database not found: /data/proceedings.db` until the user knows to pre-create the file — terrible first impression and not how a derived store of size zero should behave.

The schema itself is already idempotent — every DDL in `_BASE_SCHEMA` / `_VEC_SCHEMA` is `CREATE … IF NOT EXISTS`, and `SqliteStorage.__init__` already runs the whole script and `mkdir -p`s the parent directory. So the missing piece was purely *who* runs it on the serve path.

Two places could plausibly own the bootstrap:

1. **CLI** — keep `serve_command` in charge and have it call the schema-create helper before constructing the app.
2. **Web app** — have `create_app()` call the helper itself, on the way to opening any per-request connections that assume tables exist.

## Decision

The web layer owns it. A new `concord.storage.sqlite.ensure_schema(db_path)` function — defined alongside the existing DDL so the "schema of record" stays in one place — creates the file (and parent directory) and applies the full schema. `create_app()` calls `ensure_schema(db_path)` near the top, before any connection is opened. The CLI's pre-flight `db_path.exists()` check in `serve_command` is removed; `concord serve` against a missing DB now boots successfully into an empty app.

Out of scope for this ADR: whether `serve` should require `OPENAI_API_KEY`. The `_require_openai_key()` check stays as-is. The semantic-search path needs the key; the non-semantic browsing paths arguably don't. That's a separate decision and a separate ADR if we ever change it.

## Consequences

**Trade-offs accepted:**

- **`create_app()` now has a side effect on disk.** Constructing the app may create a SQLite file. Previously `create_app()` only read state. In practice this is the same side effect `SqliteStorage(path)` already had — the pipeline has always created the DB on first construction — so the surprise is small, and it's documented at the call site and in the ADR.
- **A typo in `--db` produces a working, empty Concord instead of an error.** Old behavior caught the typo loudly. New behavior creates `/tmp/typoo.db` and serves "no results yet" against it. Acceptable: the failure mode is visible (the user sees an empty UI and immediately questions what DB they pointed at) and the cost of the wrong choice is one empty SQLite file. Worth it for the first-run experience.
- **`concord serve` no longer signals "you forgot to run the pipeline."** Anyone who used the exit-code-2 as a reminder will lose that nudge. The empty-state UI is the new nudge.

**Things this buys:**

- **Docker first-run works without ceremony.** `docker run concord serve` against a fresh volume just works — no entrypoint scripts that pre-touch the DB, no `docker exec … concord init`. The follow-up Dockerfile PR depends on this landing.
- **The CLI's `serve` becomes a one-liner over `create_app()`.** No more redundant existence check; the layer that knows what schema it needs is also the one that creates it.
- **`ensure_schema()` is a usable primitive for future entry points.** Anything else that wants to mount a fresh SQLite (test fixtures, future bootstrap commands, a hypothetical `concord init`) can call it instead of duplicating the construct-then-close pattern.

**What stays open:**

- **`OPENAI_API_KEY` requirement for `serve`.** Still enforced by `_require_openai_key()`. Whether browsing-only deployments should be runnable without an OpenAI key — and what the search box does in that mode — is unresolved; revisit when there's a concrete deployment that wants it.
- **No "is the schema at the version this binary expects?" check.** The DDL is `IF NOT EXISTS`, so a stale-schema file produces silent missing-column errors at query time rather than a startup failure. Acceptable today because the schema is append-only across releases; if/when we introduce a destructive migration, this becomes a real migrations story (probably a `schema_version` table and a migrate step). Out of scope here.

## Rejected: CLI owns the bootstrap

`serve_command` could call `ensure_schema()` itself before constructing the app, and `create_app()` could stay pure. Rejected because it leaves a footgun: any other entry point that calls `create_app()` (tests, a future ASGI factory consumed by gunicorn / a Docker `CMD` that points at the FastAPI app object directly, an embedded usage) gets the old "tables missing" failure. Putting the bootstrap in the layer that needs the tables means there's exactly one path; nobody can construct the app wrong.

## Rejected: keep the existence check, fix it in the Dockerfile

The Docker entrypoint could `touch` the DB before invoking `concord serve`, or run a separate `concord init` first. Rejected because it pushes a Concord invariant ("the web app needs a schema") into the deployment substrate, and every new deployment shape would have to re-discover it. The web layer is the right place to own its own preconditions.
