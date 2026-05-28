"""Tests for the OpenAI embedding wrapper."""

from collections.abc import Callable
from typing import Any

import httpx
import openai
import pytest

from concord.embedding import (
    DEFAULT_MODEL,
    EMBEDDING_DIM,
    MAX_RATE_LIMIT_WAIT,
    Embedder,
    EmbeddingError,
)


class _FakeData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _FakeResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_FakeData(v) for v in vectors]


class _RecordingEmbeddings:
    """OpenAI-shaped ``.embeddings.create()`` stub that records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.canned_response: list[list[float]] | None = None

    def create(self, *, model: str, input: list[str]) -> _FakeResponse:
        self.calls.append({"model": model, "input": list(input)})
        if self.canned_response is not None:
            return _FakeResponse(self.canned_response[: len(input)])
        return _FakeResponse([[float(i)] * EMBEDDING_DIM for i in range(len(input))])


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _RecordingEmbeddings()


def _make_embedder(batch_size: int = 100) -> tuple[Embedder, _FakeClient]:
    client = _FakeClient()
    return Embedder(client, batch_size=batch_size), client


# -- construction -------------------------------------------------------------


class TestConstruction:
    def test_default_model_is_text_embedding_3_small(self) -> None:
        embedder, _ = _make_embedder()
        assert embedder.model == DEFAULT_MODEL == "text-embedding-3-small"

    def test_default_batch_size(self) -> None:
        embedder, _ = _make_embedder()
        assert embedder.batch_size == 100

    def test_zero_batch_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="batch_size must be positive"):
            Embedder(_FakeClient(), batch_size=0)

    def test_negative_batch_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="batch_size must be positive"):
            Embedder(_FakeClient(), batch_size=-5)


# -- batching ------------------------------------------------------------------


class TestBatching:
    def test_single_call_for_small_input(self) -> None:
        embedder, client = _make_embedder(batch_size=100)
        embedder.embed(["a", "b", "c"])
        assert len(client.embeddings.calls) == 1
        assert client.embeddings.calls[0]["input"] == ["a", "b", "c"]

    def test_multiple_batches_for_large_input(self) -> None:
        embedder, client = _make_embedder(batch_size=100)
        texts = [f"text-{i}" for i in range(250)]
        embedder.embed(texts)
        sizes = [len(call["input"]) for call in client.embeddings.calls]
        assert sizes == [100, 100, 50]

    def test_returns_vectors_in_input_order(self) -> None:
        embedder, client = _make_embedder(batch_size=3)
        # Canned response so we can verify ordering exactly.
        client.embeddings.canned_response = [[1.0] * EMBEDDING_DIM] * 100
        result = embedder.embed(["a", "b", "c", "d", "e"])
        assert len(result) == 5
        # All vectors have the expected dimensionality.
        assert all(len(v) == EMBEDDING_DIM for v in result)

    def test_uses_configured_model(self) -> None:
        client = _FakeClient()
        embedder = Embedder(client, model="custom-model-name")
        embedder.embed(["hello"])
        assert client.embeddings.calls[0]["model"] == "custom-model-name"

    def test_empty_input_no_api_call(self) -> None:
        embedder, client = _make_embedder()
        assert embedder.embed([]) == []
        assert client.embeddings.calls == []


# -- error handling -----------------------------------------------------------


class TestErrors:
    def test_api_exception_wrapped_in_embeddingerror(self) -> None:
        class _BrokenEmbeddings:
            def create(self, *, model: str, input: list[str]) -> Any:
                raise RuntimeError("simulated API failure")

        class _BrokenClient:
            embeddings = _BrokenEmbeddings()

        embedder = Embedder(_BrokenClient())
        with pytest.raises(EmbeddingError, match="failed for batch of 2"):
            embedder.embed(["a", "b"])

    def test_mismatched_response_length_raises(self) -> None:
        embedder, client = _make_embedder()
        # Canned response yields fewer items than inputs requested.
        client.embeddings.canned_response = [[0.0] * EMBEDDING_DIM]
        with pytest.raises(EmbeddingError, match="expected"):
            embedder.embed(["a", "b", "c"])


# -- rate-limit retry ----------------------------------------------------------


def _make_rate_limit_error(
    message: str = "Rate limit reached. Please try again in 500ms.",
    *,
    retry_after_ms: str | None = None,
    retry_after: str | None = None,
) -> openai.RateLimitError:
    """Build a real ``openai.RateLimitError`` for retry tests."""
    headers: dict[str, str] = {}
    if retry_after_ms is not None:
        headers["retry-after-ms"] = retry_after_ms
    if retry_after is not None:
        headers["retry-after"] = retry_after
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(429, headers=headers, request=request)
    return openai.RateLimitError(message, response=response, body=None)


class _RateLimitedThenOk:
    """First N calls raise rate-limit; subsequent calls succeed."""

    def __init__(self, *, fail_count: int, exc_factory: Callable[[], Exception]) -> None:
        self._remaining = fail_count
        self._exc_factory = exc_factory
        self.calls: list[dict[str, Any]] = []
        self.waits: list[float] = []

    def create(self, *, model: str, input: list[str]) -> _FakeResponse:
        self.calls.append({"model": model, "input": list(input)})
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc_factory()
        return _FakeResponse([[0.0] * EMBEDDING_DIM for _ in input])


class _Client:
    def __init__(self, embeddings: Any) -> None:
        self.embeddings = embeddings


class TestRateLimitRetry:
    def test_retries_after_rate_limit_using_retry_after_ms_header(self) -> None:
        waits: list[float] = []
        api = _RateLimitedThenOk(
            fail_count=1,
            exc_factory=lambda: _make_rate_limit_error(retry_after_ms="500"),
        )
        embedder = Embedder(_Client(api), sleep=waits.append)
        result = embedder.embed(["a"])
        assert len(result) == 1
        # 0.5s from the header + RATE_LIMIT_BUFFER (0.5s).
        assert waits == [pytest.approx(1.0)]

    def test_retries_after_rate_limit_using_retry_after_header(self) -> None:
        waits: list[float] = []
        api = _RateLimitedThenOk(
            fail_count=1,
            exc_factory=lambda: _make_rate_limit_error(retry_after="3"),
        )
        embedder = Embedder(_Client(api), sleep=waits.append)
        embedder.embed(["a"])
        assert waits == [pytest.approx(3.5)]  # 3s + 0.5s buffer

    def test_falls_back_to_message_parsing_when_no_headers(self) -> None:
        waits: list[float] = []
        api = _RateLimitedThenOk(
            fail_count=1,
            exc_factory=lambda: _make_rate_limit_error(
                message="rate limit hit, please try again in 750ms"
            ),
        )
        embedder = Embedder(_Client(api), sleep=waits.append)
        embedder.embed(["a"])
        assert waits == [pytest.approx(1.25)]  # 0.75s parsed + 0.5s buffer

    def test_falls_back_to_default_wait_when_unparseable(self) -> None:
        waits: list[float] = []
        api = _RateLimitedThenOk(
            fail_count=1,
            exc_factory=lambda: _make_rate_limit_error(message="something opaque"),
        )
        embedder = Embedder(_Client(api), sleep=waits.append)
        embedder.embed(["a"])
        # Default is 1.0s + buffer = 1.5s.
        assert waits == [pytest.approx(1.5)]

    def test_caps_unreasonable_wait_at_max(self) -> None:
        waits: list[float] = []
        # Server (hypothetically) suggests an hour. Cap to MAX_RATE_LIMIT_WAIT.
        api = _RateLimitedThenOk(
            fail_count=1,
            exc_factory=lambda: _make_rate_limit_error(retry_after="3600"),
        )
        embedder = Embedder(_Client(api), sleep=waits.append)
        embedder.embed(["a"])
        assert waits == [pytest.approx(MAX_RATE_LIMIT_WAIT)]

    def test_repeated_rate_limits_keep_retrying(self) -> None:
        waits: list[float] = []
        # 5 consecutive rate-limits, then success — no give-up budget.
        api = _RateLimitedThenOk(
            fail_count=5,
            exc_factory=lambda: _make_rate_limit_error(retry_after_ms="100"),
        )
        embedder = Embedder(_Client(api), sleep=waits.append)
        result = embedder.embed(["a"])
        assert len(result) == 1
        assert len(waits) == 5

    def test_non_rate_limit_error_still_raises_embeddingerror(self) -> None:
        # Sanity: only RateLimitError gets the retry treatment.
        class _AlwaysBroken:
            def create(self, *, model: str, input: list[str]) -> Any:
                raise RuntimeError("boom")

        embedder = Embedder(_Client(_AlwaysBroken()), sleep=lambda _: None)
        with pytest.raises(EmbeddingError, match="failed for batch"):
            embedder.embed(["a"])
