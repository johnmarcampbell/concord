"""Shared network-resilience spine for Concord HTTP clients.

The fetch layer owns retry / backoff behavior and Scrape Run recording
(ADR 0021) without inspecting response bodies. Client adapters layer any
protocol-specific parsing above it.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
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
    """How the fetch spine should treat one HTTP response."""

    PASS = "allow"  # noqa: S105 - enum label, not a secret
    THROTTLE = "throttle"
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    """Policy classification for one response."""

    disposition: Disposition
    message: str | None = None


class RateLimitPolicy:
    """Per-client rate-limit policy seam.

    The base policy is a no-op: every response falls through to the shared
    status-code handling in :class:`Fetcher`.
    """

    def before_request(self) -> None:
        return None

    def classify(self, _response: httpx.Response) -> Decision:
        return Decision(Disposition.PASS)

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
        sleep: Callable[[float], None] = time.sleep,
        source: str = "fetch",
    ) -> None:
        self._sleep = sleep
        self._consecutive_throttles = 0
        self._log = logging.getLogger(f"concord.{source}")

    def classify(self, response: httpx.Response) -> Decision:
        if response.status_code != HTTP_TOO_MANY_REQUESTS:
            return Decision(Disposition.PASS)
        delay = _retry_after_seconds(response)
        if delay is None:
            delay = _backoff_seconds(self._consecutive_throttles)
        self._log.warning("429 response; backing off %.1fs before retry", delay)
        self._sleep(delay)
        self._consecutive_throttles += 1
        return Decision(Disposition.THROTTLE)

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
    ) -> None:
        self._client = client
        self._source = source
        self._policy = policy if policy is not None else RateLimitPolicy()
        self._sleep = sleep
        self._max_transient_retries = max_transient_retries
        self._log = logging.getLogger(f"concord.{source}")

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        rec = active_recorder()
        attempts: list[Attempt] = []
        transient_attempts = 0

        while True:
            self._policy.before_request()
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                _note_attempt(attempts, transport_class=type(exc).__name__, message=str(exc))
                if transient_attempts >= self._max_transient_retries:
                    _record_failure(rec, self._source, path, attempts)
                    raise FetchError(
                        f"transport error calling {path} "
                        f"(gave up after {self._max_transient_retries} attempts): {exc}"
                    ) from exc
                delay = _backoff_seconds(transient_attempts)
                self._log.warning("transport error on %s (%s); retrying in %.1fs", path, exc, delay)
                self._sleep(delay)
                transient_attempts += 1
                continue

            decision = self._policy.classify(response)
            if decision.disposition is Disposition.THROTTLE:
                _note_attempt(
                    attempts,
                    status=response.status_code,
                    message=_attempt_message(response, decision.message),
                )
                continue
            if decision.disposition is Disposition.REJECT:
                _note_attempt(
                    attempts,
                    status=response.status_code,
                    message=_attempt_message(response, decision.message),
                )
                _record_failure(rec, self._source, path, attempts)
                raise FetchError(
                    _rejected_message(path, response, decision.message),
                    status_code=response.status_code,
                )

            status = response.status_code
            if HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX:
                _note_attempt(attempts, status=status, message=_attempt_message(response))
                if transient_attempts >= self._max_transient_retries:
                    _record_failure(rec, self._source, path, attempts)
                    raise FetchError(
                        f"{status} {_attempt_message(response)} from {path} "
                        f"(gave up after {self._max_transient_retries} attempts)",
                        status_code=status,
                    )
                delay = _backoff_seconds(transient_attempts)
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
            return response.content


def _attempt_message(response: httpx.Response, message: str | None = None) -> str:
    if message is not None:
        return message
    return response.reason_phrase or f"HTTP {response.status_code}"


def _rejected_message(path: str, response: httpx.Response, message: str | None) -> str:
    detail = _attempt_message(response, message)
    if response.is_success:
        return f"{detail} from {path} ({response.status_code})"
    return f"{response.status_code} {detail} from {path}"


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
