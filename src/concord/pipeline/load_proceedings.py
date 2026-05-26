"""Pipeline orchestrator.

The glue between the API client, the text fetcher, and storage. Given a date
range, :func:`pull` produces one :class:`Proceeding` per article for every
issue whose ``issue_date`` lies in ``[start, end]`` and writes the results
through the supplied :class:`Storage` backend.

Design notes
------------

* The ``/v3/daily-congressional-record`` list endpoint has **no date filter**,
  and the API does not return results in strict ``issueDate`` order: recently
  updated issues bubble to the top regardless of when they were originally
  issued. (E.g., in the captured fixture, 2026-05-22 → 2026-05-20 → 2026-05-21.)
  For correctness, :func:`pull` walks every page of the list endpoint rather
  than trying to short-circuit on the first out-of-range date.
* The walk uses the API's maximum page size to keep the metadata cost low: a
  full 30-year backfill is ~24 list calls plus one ``/articles`` call per
  issue, well under the 5000-requests/hour rate limit.
* ``storage.has(granule_id)`` is consulted before each text fetch, so
  re-running ``pull`` over an already-pulled range is a no-op (no HTTP for
  the article text, no write). This is the resume contract the storage
  backend in #20 was designed to provide.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import NamedTuple

from concord.api import Client
from concord.models import Issue, Proceeding
from concord.storage.base import Storage
from concord.text import TextFetchError

_log = logging.getLogger("concord.pipeline")

#: Per-page size when paginating ``list_issues``. The API caps at 250;
#: using the max minimizes round trips on long-range pulls.
LIST_PAGE_SIZE = 250


class PullResult(NamedTuple):
    """Outcome of a single :func:`pull` invocation.

    ``written``  — new :class:`Proceeding` records persisted this call.
    ``skipped``  — articles already in storage (matched by ``granule_id``);
                   their text fetch was avoided.
    ``failed``   — articles whose text fetch raised :class:`TextFetchError`
                   and were skipped so the rest of the run could continue.
                   These will be re-attempted on the next ``pull`` since
                   they were never written to storage.
    """

    written: int
    skipped: int
    failed: int = 0


class ProgressEvent(NamedTuple):
    """A single progress notification emitted after each in-range issue."""

    issue: Issue
    issue_written: int
    issue_skipped: int
    issue_failed: int
    total_written: int
    total_skipped: int
    total_failed: int


def pull(  # noqa: C901 — pipeline orchestrator
    start: date,
    end: date,
    *,
    client: Client,
    fetch: Callable[[str], str],
    storage: Storage,
    limit: int | None = None,
    progress: Callable[[ProgressEvent], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PullResult:
    """Pull every in-range article and persist it as a :class:`Proceeding`.

    Resume contract
    ---------------

    The pipeline is crash-safe by construction. Storage writes one Proceeding
    at a time and the dedup index is rebuilt from disk on the next run, so
    killing the process at any point loses at most the in-flight article;
    the next invocation picks up only what's missing. Callers don't need a
    special "resume" mode — re-running the same ``pull(start, end, ...)`` is
    the resume.

    Parameters
    ----------
    start, end:
        Inclusive date bounds. ``end >= start`` is the caller's responsibility;
        if ``end < start`` the function returns ``PullResult(0, 0)`` without
        making any API calls.
    client:
        Wired :class:`concord.api.Client`.
    fetch:
        Callable taking a Formatted Text URL and returning the plain text of
        the article. Typically a closure over :func:`concord.text.fetch_text`
        bound to an ``httpx.Client``.
    storage:
        Any :class:`Storage` implementation. ``has`` is checked before every
        article to skip already-stored work; ``write`` is called for each new
        :class:`Proceeding`.
    limit:
        Cap on total writes. ``None`` (the default) means unlimited. Useful
        for smoke tests and dry runs.
    progress:
        Optional callback invoked once per in-range issue *after* its
        articles have been processed (written or skipped). Use this to print
        a heartbeat line on long backfills. The pipeline itself prints
        nothing — formatting is the caller's choice.
    now:
        Injection point for ``datetime.now(UTC)`` used to stamp
        ``fetched_at``. Lets tests assert exact timestamps.

    Returns
    -------
    PullResult
        ``(written, skipped)`` counts for this call.
    """
    if end < start:
        return PullResult(written=0, skipped=0, failed=0)

    written = 0
    skipped = 0
    failed = 0
    offset = 0
    while True:
        issues, next_offset = client.list_issues(limit=LIST_PAGE_SIZE, offset=offset)
        for issue in issues:
            if not (start <= issue.issue_date <= end):
                continue

            issue_written = 0
            issue_skipped = 0
            issue_failed = 0
            hit_limit = False
            for article in client.list_articles(issue.volume, issue.issue_number):
                if storage.has(article.granule_id):
                    skipped += 1
                    issue_skipped += 1
                    continue
                try:
                    text = fetch(str(article.text_url))
                except TextFetchError as exc:
                    # Single article failed to fetch (timeout, 5xx, etc.).
                    # Log + skip + continue — the run as a whole should not
                    # die because one article is temporarily unreachable.
                    # The next `concord pull` over the same range will
                    # retry this granule since it was never written.
                    _log.warning(
                        "skipping %s after fetch failure: %s",
                        article.granule_id,
                        exc,
                    )
                    failed += 1
                    issue_failed += 1
                    continue
                proceeding = Proceeding.build(
                    issue=issue,
                    article=article,
                    text=text,
                    fetched_at=now(),
                )
                storage.write(proceeding)
                written += 1
                issue_written += 1
                if limit is not None and written >= limit:
                    hit_limit = True
                    break

            if progress is not None:
                progress(
                    ProgressEvent(
                        issue=issue,
                        issue_written=issue_written,
                        issue_skipped=issue_skipped,
                        issue_failed=issue_failed,
                        total_written=written,
                        total_skipped=skipped,
                        total_failed=failed,
                    )
                )

            if hit_limit:
                return PullResult(written=written, skipped=skipped, failed=failed)
        if next_offset is None:
            return PullResult(written=written, skipped=skipped, failed=failed)
        offset = next_offset
