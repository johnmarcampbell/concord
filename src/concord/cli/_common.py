"""Shared utilities, constants, and the Progress helper for the Concord CLI."""

import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

import typer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

#: How often (in lines processed) the load command emits a progress line.
LOAD_PROGRESS_EVERY = 100

#: Default SQLite derived-store path.
DEFAULT_DB = Path("./data/proceedings.db")


# ---------------------------------------------------------------------------
# Progress helper
# ---------------------------------------------------------------------------


class Progress:
    """Progress lines that overwrite themselves on a TTY.

    On an interactive terminal, ``update`` rewrites the same line via
    carriage-return + clear-to-end-of-line, so a long pull doesn't scroll
    the screen with one row per issue. On a non-TTY stream (cron logs,
    pipes, file redirection) it falls back to one line per update so the
    log file still has a useful record.

    Call ``commit`` between phases to end the in-place line with a newline
    — the next ``update`` then starts a fresh line. ``close`` is the same
    thing; use whichever reads better in context.
    """

    def __init__(self, enabled: bool, *, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = enabled
        self._inplace = enabled and self._stream.isatty()
        self._open = False

    def update(self, msg: str) -> None:
        if not self._enabled:
            return
        if self._inplace:
            # \r to start of line, \x1b[K to clear from cursor to end so a
            # shorter message doesn't leave debris from the previous one.
            self._stream.write("\r\x1b[K" + msg)
            self._open = True
        else:
            self._stream.write(msg + "\n")
        self._stream.flush()

    def commit(self) -> None:
        """End the current in-place line so subsequent output starts fresh."""
        if self._open:
            self._stream.write("\n")
            self._stream.flush()
            self._open = False

    # Context-manager sugar so callers can `with Progress(...) as p:`
    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *_a: object) -> None:
        self.commit()

    @property
    def interactive(self) -> bool:
        """True when progress is enabled and the stream is a TTY."""
        return self._inplace


class RateTracker:
    """Tracks throughput rate and computes ETA for CLI progress lines.

    Typical usage inside a ``_on_progress`` closure::

        tracker = RateTracker(total=known_total)   # total may be None

        def _on_progress(event):
            if event.is_done:
                progress.update(label + tracker.finish(event.count))
                progress.commit()
                tracker.reset()
            else:
                tracker.update_total(event.category_total)  # safe to call every time; idempotent
                progress.update(label + tracker.tick(event.count))
    """

    def __init__(self, total: int | None = None) -> None:
        self._total = total
        self._start: float | None = None

    def update_total(self, total: int | None) -> None:
        """Set or refine the total when first learned (e.g. from first API page)."""
        if total is not None:
            self._total = total

    def tick(self, done: int) -> str:
        """Record *done* items complete; return a formatted progress string.

        Format: ``"  412 / 5,021    18/min   ETA 4h 17m"``
        Before the first second elapses the rate/ETA portion is omitted.
        """
        now = time.monotonic()
        if self._start is None:
            self._start = now
        elapsed = now - self._start

        count = f"{done:,} / {self._total:,}" if self._total is not None else f"{done:,}"

        parts: list[str] = [count]

        if elapsed >= 1.0 and done > 0:
            rate = done / elapsed
            parts.append(_fmt_rate(rate))
            if self._total is not None and done < self._total:
                eta_secs = (self._total - done) / rate
                parts.append(f"ETA {_fmt_duration(eta_secs)}")

        return "   ".join(parts)

    def finish(self, done: int) -> str:
        """Return a completion string: ``"5,021 written   [47m 12s]"``."""
        now = time.monotonic()
        elapsed = (now - self._start) if self._start is not None else 0.0
        return f"{done:,} written   [{_fmt_duration(elapsed)}]"

    def reset(self) -> None:
        """Reset timing and total for reuse on the next category."""
        self._start = None
        self._total = None


_SECS_PER_MIN: int = 60
_SECS_PER_HOUR: int = 3_600


def _fmt_rate(rate: float) -> str:
    """Format items/sec as a human-readable rate string."""
    if rate >= 1.0:
        return f"{rate:.0f}/s"
    if rate * _SECS_PER_MIN >= 1.0:
        return f"{rate * _SECS_PER_MIN:.0f}/min"
    return f"{rate * _SECS_PER_HOUR:.0f}/hr"


def _fmt_duration(secs: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    s = int(secs)
    if s < _SECS_PER_MIN:
        return f"{s}s"
    if s < _SECS_PER_HOUR:
        m, sec = divmod(s, _SECS_PER_MIN)
        return f"{m}m {sec:02d}s"
    h, rem = divmod(s, _SECS_PER_HOUR)
    return f"{h}h {rem // _SECS_PER_MIN:02d}m"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _require_openai_key() -> None:
    if not os.environ.get(ENV_OPENAI_API_KEY):
        typer.echo(
            f"error: {ENV_OPENAI_API_KEY} is not set; required for embedding calls",
            err=True,
        )
        raise typer.Exit(code=2)


def _parse_csv(raw: str, *, name: str, coerce: "Callable[[str], Any]") -> "list[Any]":
    """Parse a comma-separated CLI option into a list of values.

    ``coerce`` is applied to each non-empty token (e.g. ``int`` for
    ``--congresses``, ``str.lower`` for ``--bill-types``). ``name`` is
    used in error messages.
    """
    out: list[Any] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            out.append(coerce(token))
        except (TypeError, ValueError) as exc:
            raise typer.BadParameter(f"bad value in --{name}: {token!r}") from exc
    if not out:
        raise typer.BadParameter(f"no values parsed from --{name} {raw!r}")
    return out


def _parse_congresses(raw: str) -> list[int]:
    """Parse ``--congresses 117,118,119`` into ``[117, 118, 119]``."""
    return _parse_csv(raw, name="congresses", coerce=int)
