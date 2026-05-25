"""OpenAI embedding client wrapper.

Wraps an injected :class:`openai.OpenAI` so production and tests share the
same surface. The OpenAI SDK already handles retries and rate-limit backoff
internally; this module just batches calls and surfaces a typed error on
unrecoverable failures.

Single model by design: ``text-embedding-3-small`` (1536 dims). Per
ADR-0004, swapping models requires re-embedding the entire corpus, which is
a manual operation (``DELETE FROM chunks_vec`` then re-run ``concord index``).
"""

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

#: Embedding dimension for ``text-embedding-3-small``.
EMBEDDING_DIM = 1536

#: Default model name. Centralized here so ADR-0004 has one place to live.
DEFAULT_MODEL = "text-embedding-3-small"


class EmbeddingError(Exception):
    """Raised when OpenAI returns an unrecoverable error for an embedding batch."""


class _EmbeddingsAPI(Protocol):
    """Minimal slice of ``openai.OpenAI.embeddings`` the Embedder uses.

    Defined as a Protocol so tests can pass a tiny stub object without
    needing to construct a real ``openai.OpenAI`` instance.
    """

    def create(self, *, model: str, input: list[str]) -> Any: ...


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
    """

    def __init__(
        self,
        client: _OpenAILike,
        *,
        model: str = DEFAULT_MODEL,
        batch_size: int = 100,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._client = client
        self._model = model
        self._batch_size = batch_size

    @property
    def model(self) -> str:
        return self._model

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding per input text, in input order.

        Batches into groups of ``batch_size`` under the hood. The returned
        list is flat; ``len(result) == len(texts)``.
        """
        if not texts:
            return []

        out: list[list[float]] = []
        for batch_start in range(0, len(texts), self._batch_size):
            batch = texts[batch_start : batch_start + self._batch_size]
            try:
                response = self._client.embeddings.create(model=self._model, input=batch)
            except Exception as exc:
                raise EmbeddingError(
                    f"OpenAI embeddings.create failed for batch of {len(batch)} inputs"
                ) from exc
            for item in response.data:
                out.append(list(item.embedding))
        if len(out) != len(texts):
            raise EmbeddingError(f"expected {len(texts)} embeddings from API, got {len(out)}")
        return out
