"""Tests for the OpenAI embedding wrapper."""

from typing import Any

import pytest

from concord.embedding import DEFAULT_MODEL, EMBEDDING_DIM, Embedder, EmbeddingError


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
