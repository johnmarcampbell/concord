"""OpenAI embedding client wrapper.

Wraps an injected :class:`openai.OpenAI` so production and tests share the
same surface. Single model by design: ``text-embedding-3-small`` (1536
dims). Per ADR-0004, swapping models requires re-embedding the entire
corpus (``DELETE FROM chunks_vec`` then re-run ``concord index``).

Rate-limit handling
-------------------

The OpenAI SDK has its own internal retry logic but gives up after a few
attempts. For a multi-hour index pass that's not enough — a single
``RateLimitError`` would abort the whole run. This module wraps every
``embeddings.create`` call in its own retry loop that:

- catches :class:`openai.RateLimitError`,
- reads the ``Retry-After`` (or ``Retry-After-Ms``) header on the
  response when present, falling back to parsing the error message,
  falling back to an exponential backoff,
- sleeps that long plus :data:`RATE_LIMIT_BUFFER` seconds (insurance
  against clock skew between us and the OpenAI side),
- logs an INFO message naming the wait time so a long index pass
  reports its own pauses,
- retries indefinitely. A 429 is "wait", not "fail".

Other exceptions (network errors, 5xx, malformed responses) surface as
:class:`EmbeddingError`.
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

#: Embedding dimension for ``text-embedding-3-small``.
EMBEDDING_DIM = 1536

#: Default model name. Centralized here so ADR-0004 has one place to live.
DEFAULT_MODEL = "text-embedding-3-small"

#: Extra seconds added to the server-suggested wait before retrying. Buys
#: us insurance against clock skew or the server's quota window closing a
#: hair after the suggested time.
RATE_LIMIT_BUFFER = 0.5

#: Cap on a single rate-limit wait. Servers occasionally suggest absurdly
#: long waits (rare, but worth bounding). 5 minutes is "patient" without
#: being "give up and die in cron."
MAX_RATE_LIMIT_WAIT = 300.0

_log = logging.getLogger("concord.embedding")

# OpenAI's rate-limit error messages look like
# "Please try again in 579ms." or "Please try again in 1.2s."
_RETRY_AFTER_MESSAGE_RE = re.compile(r"try again in\s+([\d.]+)\s*(ms|s)\b", re.IGNORECASE)


class EmbeddingError(Exception):
    """Raised when OpenAI returns an unrecoverable error for an embedding batch."""


class _EmbeddingsAPI(Protocol):
    """Minimal slice of ``openai.OpenAI.embeddings`` the Embedder uses.

    Defined as a Protocol so tests can pass a tiny stub object without
    needing to construct a real ``openai.OpenAI`` instance.
    """

    def create(self, *, model: str, input: list[str]) -> Any: ...  # noqa: A002 — matches openai SDK kwarg name


class _OpenAILike(Protocol):
    @property
    def embeddings(self) -> _EmbeddingsAPI: ...


class Embedder:
    """Batched embedder over an injected OpenAI-compatible client.

    Parameters
    ----------
    client:
        An instance of :class:`openai.OpenAI` (or a stub with a matching
        ``client.embeddings.create(model=..., input=[...])`` surface).
    model:
        Model name. Defaults to :data:`DEFAULT_MODEL`.
    batch_size:
        Number of texts per API call. The OpenAI API accepts up to 2048
        inputs per request; 100 is comfortably under that and balances
        throughput against the blast radius of a single failed batch.
    sleep:
        Callable invoked when a rate-limit retry needs to wait. Defaults
        to :func:`time.sleep`; tests inject a no-op.
    """

    def __init__(
        self,
        client: _OpenAILike,
        *,
        model: str = DEFAULT_MODEL,
        batch_size: int = 100,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._client = client
        self._model = model
        self._batch_size = batch_size
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding per input text, in input order.

        Batches into groups of ``batch_size`` under the hood. The returned
        list is flat; ``len(result) == len(texts)``. Rate-limit errors
        from OpenAI are caught and waited on per the server's
        ``Retry-After`` header; the call is retried indefinitely on 429.
        """
        if not texts:
            return []

        out: list[list[float]] = []
        for batch_start in range(0, len(texts), self._batch_size):
            batch = texts[batch_start : batch_start + self._batch_size]
            response = self._create_with_retry(batch)
            for item in response.data:
                out.append(list(item.embedding))
        if len(out) != len(texts):
            raise EmbeddingError(f"expected {len(texts)} embeddings from API, got {len(out)}")
        return out

    # -- internals --------------------------------------------------------

    def _create_with_retry(self, batch: list[str]) -> Any:
        """Call ``embeddings.create`` once, retrying indefinitely on 429."""
        # Lazy import: openai is a runtime dep but we don't want test stubs
        # to need it on the import path. The Exception type comparison
        # below falls back to class-name matching when openai isn't loaded.
        try:
            import openai  # noqa: PLC0415 — guarded so test stubs don't need openai installed

            rate_limit_exc: type[BaseException] = openai.RateLimitError
        except ImportError:  # pragma: no cover - openai is a hard dep in prod
            rate_limit_exc = type("_NeverMatches", (BaseException,), {})

        attempt = 0
        while True:
            try:
                return self._client.embeddings.create(model=self._model, input=batch)
            except rate_limit_exc as exc:
                wait = _retry_after_seconds(exc) + RATE_LIMIT_BUFFER
                wait = min(wait, MAX_RATE_LIMIT_WAIT)
                _log.info(
                    "openai rate-limited (batch of %d); waiting %.2fs before retry",
                    len(batch),
                    wait,
                )
                self._sleep(wait)
                attempt += 1
                continue
            except Exception as exc:
                # Defensive fallback if openai wasn't importable above:
                # match RateLimitError by class name.
                if exc.__class__.__name__ == "RateLimitError":
                    wait = _retry_after_seconds(exc) + RATE_LIMIT_BUFFER
                    wait = min(wait, MAX_RATE_LIMIT_WAIT)
                    _log.info(
                        "openai rate-limited (batch of %d); waiting %.2fs before retry",
                        len(batch),
                        wait,
                    )
                    self._sleep(wait)
                    attempt += 1
                    continue
                raise EmbeddingError(
                    f"OpenAI embeddings.create failed for batch of {len(batch)} inputs"
                ) from exc


# -- helpers ---------------------------------------------------------------


def _retry_after_seconds(exc: BaseException) -> float:
    """Best-effort extraction of "wait this many seconds" from a 429.

    Order of preference:
    1. ``response.headers["retry-after-ms"]`` (OpenAI-specific, milliseconds)
    2. ``response.headers["retry-after"]`` (HTTP standard, seconds)
    3. Regex over the error message for ``try again in Xms|Xs``
    4. Default: 1.0 second
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        ms = headers.get("retry-after-ms") or headers.get("Retry-After-Ms")
        if ms is not None:
            try:
                return float(ms) / 1000.0
            except (TypeError, ValueError):
                pass
        secs = headers.get("retry-after") or headers.get("Retry-After")
        if secs is not None:
            try:
                return float(secs)
            except (TypeError, ValueError):
                pass

    message = str(exc)
    match = _RETRY_AFTER_MESSAGE_RE.search(message)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        return value / 1000.0 if unit == "ms" else value

    return 1.0
