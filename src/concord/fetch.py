"""Shared network-resilience spine for Concord HTTP clients.

The fetch layer owns retry / backoff behavior and Scrape Run recording
(ADR 0021) without inspecting response bodies. Client adapters layer any
protocol-specific parsing above it.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

import httpx

from concord.models.runs import Attempt
from concord.observability import Recorder, active_recorder

MAX_BACKOFF = 60.0
MAX_5XX_RETRIES = 5
_BACKOFF_BASE = 2.0

HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 600  # exclusive upper bound


class FetchError(Exception):
    """Raised when a fetch fails before the caller can inspect the body."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Disposition(Enum):
    """How the fetch spine should treat one HTTP response.

    ``ALLOW`` falls through to the spine's status-code handling; ``THROTTLE``
    retries after a cooldown (the rate-limit path); ``REJECT`` fails the fetch
    terminally even on a 2xx, for a response the policy knows is unusable (e.g.
    senate.gov's HTML-disguised-as-200 not-found trap).
    """

    ALLOW = auto()
    THROTTLE = auto()
    REJECT = auto()


@dataclass(frozen=True)
class Decision:
    """Pure response-policy decision for the next fetch step.

    ``delay`` is the cooldown a ``THROTTLE`` decision defers to the spine (0 if
    the policy slept it itself). ``message`` labels a ``REJECT`` — it becomes
    the recorded attempt's message (e.g. a sentinel marker) and seeds the raised
    :class:`FetchError`.
    """

    disposition: Disposition
    delay: float = 0.0
    message: str | None = None


class RateLimitPolicy:
    """Per-client rate-limit policy seam.

    The base policy is a no-op: every response falls through to the shared
    status-code handling in :class:`Fetcher`.

    ``before_request`` runs once per logical fetch before the first attempt.
    ``on_response`` may mutate policy state, then returns a decision the fetch
    spine acts on. A ``THROTTLE`` decision may either carry the cooldown as its
    ``delay`` for the spine to sleep (as :class:`RetryAfterPolicy` does), or
    perform the wait itself and return ``delay=0`` when the policy owns its own
    clock (as ``text.py``'s ``AdaptiveThrottlePolicy`` does); the spine only
    sleeps a non-zero ``delay``. A ``REJECT`` decision fails the fetch
    terminally even on a 2xx (as ``senate_xml.py``'s ``SenateSentinelPolicy``
    does for the HTML not-found trap), recording ``message`` on the attempt.
    """

    def before_request(self) -> None:
        return None

    def on_response(self, _path: str, _response: httpx.Response) -> Decision:
        return Decision(Disposition.ALLOW)

    def on_success(self) -> None:
        return None


class RetryAfterPolicy(RateLimitPolicy):
    """HTTP 429 handling for api.congress.gov.

    Honors ``Retry-After`` when present; otherwise backs off exponentially on
    consecutive 429s without consuming the transient 5xx budget.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._consecutive_throttles = 0
        self._log = logger if logger is not None else logging.getLogger("concord.fetch")

    def on_response(self, path: str, response: httpx.Response) -> Decision:
        if response.status_code != HTTP_TOO_MANY_REQUESTS:
            return Decision(Disposition.ALLOW)
        delay = _retry_after_seconds(response)
        if delay is None:
            delay = _backoff_seconds(self._consecutive_throttles)
        self._log.warning("429 from %s; backing off %.1fs before retry", path, delay)
        self._consecutive_throttles += 1
        return Decision(Disposition.THROTTLE, delay)

    def on_success(self) -> None:
        self._consecutive_throttles = 0


class Fetcher:
    """Retrying network fetcher over a caller-supplied ``httpx.Client``."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        source: str,
        policy: RateLimitPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_transient_retries: int = MAX_5XX_RETRIES,
        follow_redirects: bool = False,
    ) -> None:
        self._client = client
        self._source = source
        self._policy = policy if policy is not None else RateLimitPolicy()
        self._sleep = sleep
        self._max_transient_retries = max_transient_retries
        # Redirect-following is a per-tier concern: api.congress.gov never
        # redirects (default off), but the congress.gov text tier does, so the
        # text client opts in rather than mutating the caller's httpx.Client.
        self._follow_redirects = follow_redirects
        self._log = logging.getLogger(f"concord.{source}")

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        rec = active_recorder()
        attempts: list[Attempt] = []
        transient_attempts = 0

        self._policy.before_request()
        while True:
            try:
                response = self._client.get(
                    path, params=params, follow_redirects=self._follow_redirects
                )
            except httpx.HTTPError as exc:
                _note_attempt(attempts, transport_class=type(exc).__name__, message=str(exc))
                delay = self._next_transient_delay_or_raise(
                    rec=rec,
                    path=path,
                    attempts=attempts,
                    transient_attempts=transient_attempts,
                    failure_message=(
                        f"transport error calling {path} "
                        f"(gave up after {self._max_transient_retries} attempts): {exc}"
                    ),
                    cause=exc,
                )
                self._log.warning("transport error on %s (%s); retrying in %.1fs", path, exc, delay)
                self._sleep(delay)
                transient_attempts += 1
                continue

            decision = self._policy.on_response(path, response)
            if decision.disposition is Disposition.THROTTLE:
                _note_attempt(
                    attempts, status=response.status_code, message=_attempt_message(response)
                )
                # A policy that owns its own cooldown wait (e.g. text.py's
                # AdaptiveThrottle, which sleeps inside penalize) returns a zero
                # delay; only sleep when the policy defers the wait to the spine.
                if decision.delay:
                    self._sleep(decision.delay)
                continue

            if decision.disposition is Disposition.REJECT:
                # The policy vetoed an otherwise-2xx response it knows is
                # unusable (e.g. senate.gov's HTML not-found trap): record it as
                # a terminal failure, never a success, carrying the policy's
                # marker so the cause is legible in the ledger.
                reason = decision.message or _attempt_message(response)
                _note_attempt(attempts, status=response.status_code, message=reason)
                _record_failure(rec, self._source, path, attempts)
                raise FetchError(f"{reason} from {path}", status_code=response.status_code)

            status = response.status_code
            if HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX:
                _note_attempt(attempts, status=status, message=_attempt_message(response))
                delay = self._next_transient_delay_or_raise(
                    rec=rec,
                    path=path,
                    attempts=attempts,
                    transient_attempts=transient_attempts,
                    failure_message=(
                        f"{status} {_attempt_message(response)} from {path} "
                        f"(gave up after {self._max_transient_retries} attempts)"
                    ),
                    status_code=status,
                )
                self._log.warning(
                    "%s from %s; retrying in %.1fs (attempt %d/%d)",
                    status,
                    path,
                    delay,
                    transient_attempts + 1,
                    self._max_transient_retries,
                )
                self._sleep(delay)
                transient_attempts += 1
                continue

            if not response.is_success:
                _note_attempt(attempts, status=status, message=_attempt_message(response))
                _record_failure(rec, self._source, path, attempts)
                raise FetchError(
                    f"{status} {_attempt_message(response)} from {path}",
                    status_code=status,
                )

            self._policy.on_success()
            _record_success(rec, self._source, path, attempts)
            return response

    def _next_transient_delay_or_raise(
        self,
        *,
        rec: Recorder | None,
        path: str,
        attempts: list[Attempt],
        transient_attempts: int,
        failure_message: str,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> float:
        if transient_attempts >= self._max_transient_retries:
            _record_failure(rec, self._source, path, attempts)
            error = FetchError(failure_message, status_code=status_code)
            if cause is not None:
                raise error from cause
            raise error
        return _backoff_seconds(transient_attempts)


def _attempt_message(response: httpx.Response) -> str:
    return response.reason_phrase or f"HTTP {response.status_code}"


def _note_attempt(
    attempts: list[Attempt],
    *,
    status: int | None = None,
    transport_class: str | None = None,
    message: str,
) -> None:
    attempts.append(
        Attempt(
            n=len(attempts) + 1,
            status=status,
            transport_class=transport_class,
            message=message,
        )
    )


def _record_success(
    rec: Recorder | None,
    source: str,
    path: str,
    attempts: list[Attempt],
) -> None:
    if rec is None:
        return
    rec.note_success(source, path)
    if attempts:
        rec.note_request_outcome(source, path, attempts, resolved=True)


def _record_failure(
    rec: Recorder | None,
    source: str,
    path: str,
    attempts: list[Attempt],
) -> None:
    if rec is not None:
        rec.note_request_outcome(source, path, attempts, resolved=False)


def _backoff_seconds(attempt: int) -> float:
    return min(_BACKOFF_BASE**attempt, MAX_BACKOFF)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_BACKOFF)
