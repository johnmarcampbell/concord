"""Jinja template filters for the web layer.

Holds the presentation-only filters the templates rely on and the single
:func:`register` seam that ``create_app`` calls to wire them onto the
Jinja environment. The pure helpers they delegate to live in core
(:func:`concord._common.ordinal`); this module only owns the
web-presentation ``humanize_age`` and the registration glue.
"""

from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from concord._common import ordinal

#: Coarse buckets used by :func:`humanize_age`. Order matters: each tuple
#: is ``(threshold_in_seconds, singular_unit_seconds, unit_name)``; the
#: first whose threshold the elapsed time crosses determines the unit.
_AGE_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (60, 1, "second"),
    (3600, 60, "minute"),
    (86_400, 3600, "hour"),
    (2_592_000, 86_400, "day"),
    (31_536_000, 2_592_000, "month"),
    (10**18, 31_536_000, "year"),
)

#: Threshold (in seconds) below which we collapse to "just now".
_JUST_NOW_SECONDS = 30


def humanize_age(value: str | datetime | None, *, now: datetime | None = None) -> str:
    """Render an ISO 8601 timestamp as a coarse "N units ago" string.

    Returns ``"just now"`` for ages under 30 seconds, ``"in the future"``
    for future timestamps (clock skew), and an empty string for ``None``
    or unparseable input. Used by the Bill profile to label each tier-2
    section's last-fetched moment.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return ""
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    delta = (current - parsed).total_seconds()
    if delta < 0:
        return "in the future"
    if delta < _JUST_NOW_SECONDS:
        return "just now"
    for threshold, unit_seconds, unit_name in _AGE_BUCKETS:
        if delta < threshold:
            count = int(delta // unit_seconds)
            plural = "" if count == 1 else "s"
            return f"{count} {unit_name}{plural} ago"
    return ""


def register(templates: Jinja2Templates) -> None:
    """Wire the web layer's Jinja filters onto ``templates``.

    Called once by ``create_app``. ``ordinal`` is the shared core helper
    (used for Congress numbers); ``humanize_age`` is web-only.
    """
    templates.env.filters["humanize_age"] = humanize_age
    templates.env.filters["ordinal"] = ordinal
