"""Recursive token-aware text splitter.

Splits a proceeding's text into overlapping chunks sized for the OpenAI
``text-embedding-3-small`` model. The algorithm is a small recursive
character splitter modelled after LangChain's
``RecursiveCharacterTextSplitter`` but kept dependency-light: we own
~150 lines and don't pull in ``langchain-core``. Token counts come from
:mod:`tiktoken` using the ``cl100k_base`` encoding (what
``text-embedding-3-small`` uses).

The output is a list of :class:`Chunk` objects. Each carries:

- ``chunk_index``: 0-based position within the parent proceeding.
- ``text``: the chunk's text, taken directly from the parent so original
  whitespace and separators are preserved.
- ``char_start`` / ``char_end``: the chunk's span in the parent text, so
  the web layer can later highlight matched regions in the full doc view.

Parameters (size, overlap, encoding) come from :class:`ChunkerConfig`,
which carries the values established in ADR-0005 as defaults.
"""

from dataclasses import dataclass

import tiktoken
from pydantic import BaseModel

# Separator hierarchy: from biggest semantic boundary to smallest. The
# empty string at the end forces a hard token-level split as a last resort
# for pathological input (one giant word, etc.).
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ", "")


@dataclass(frozen=True)
class ChunkerConfig:
    """Knobs for :class:`Chunker`. Defaults match ADR-0005."""

    chunk_size: int = 512
    overlap: int = 100
    encoding_name: str = "cl100k_base"


class Chunk(BaseModel):
    """One chunk of a proceeding's text, the unit of search retrieval."""

    chunk_index: int
    text: str
    char_start: int
    char_end: int


class Chunker:
    """Split text into overlapping, token-bounded chunks."""

    def __init__(self, config: ChunkerConfig | None = None) -> None:
        self._config = config or ChunkerConfig()
        if self._config.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self._config.overlap < 0:
            raise ValueError("overlap must be non-negative")
        if self._config.overlap >= self._config.chunk_size:
            raise ValueError("overlap must be less than chunk_size")
        self._enc = tiktoken.get_encoding(self._config.encoding_name)

    @property
    def config(self) -> ChunkerConfig:
        return self._config

    def chunk(self, text: str) -> list[Chunk]:
        """Return the chunks for ``text``.

        Empty or whitespace-only input returns ``[]``.
        """
        if not text or not text.strip():
            return []

        splitlets = self._hierarchical_split(text, 0, _SEPARATORS)
        return self._merge_with_overlap(text, splitlets)

    # -- phase 1: hierarchical split --------------------------------------

    def _hierarchical_split(
        self,
        text: str,
        base_offset: int,
        separators: tuple[str, ...],
    ) -> list[tuple[str, int, int]]:
        """Split ``text`` into pieces each at or below ``chunk_size`` tokens.

        Tries separators in order. A piece that already fits is kept as-is;
        an oversized piece is recursively split with the remaining
        separators (finer boundary). Final fallback (empty separator) is a
        hard token split.

        Returns a flat list of ``(piece_text, char_start, char_end)`` with
        offsets relative to the original (un-substringed) text.
        """
        if self._token_count(text) <= self._config.chunk_size:
            return [(text, base_offset, base_offset + len(text))]

        if not separators:
            return self._hard_token_split(text, base_offset)

        sep = separators[0]
        remaining = separators[1:]

        if sep == "":
            return self._hard_token_split(text, base_offset)

        result: list[tuple[str, int, int]] = []
        pos = 0
        for piece in text.split(sep):
            piece_start = base_offset + pos
            piece_end = piece_start + len(piece)
            if piece:
                if self._token_count(piece) <= self._config.chunk_size:
                    result.append((piece, piece_start, piece_end))
                else:
                    result.extend(self._hierarchical_split(piece, piece_start, remaining))
            pos += len(piece) + len(sep)
        return result

    def _hard_token_split(self, text: str, base_offset: int) -> list[tuple[str, int, int]]:
        """Slice ``text`` into ``chunk_size``-token spans, find each in the source."""
        tokens = self._enc.encode(text)
        pieces: list[tuple[str, int, int]] = []
        cursor = 0  # character cursor in `text`
        i = 0
        while i < len(tokens):
            window_tokens = tokens[i : i + self._config.chunk_size]
            window_text = self._enc.decode(window_tokens)
            # Find this decoded span at or after the cursor. Tokenization is
            # lossless for cl100k_base, so the decoded text should appear
            # contiguously in the source.
            local_idx = text.find(window_text, cursor)
            if local_idx < 0:
                # Defensive fallback: use the cursor.
                local_idx = cursor
            pieces.append(
                (window_text, base_offset + local_idx, base_offset + local_idx + len(window_text))
            )
            cursor = local_idx + len(window_text)
            i += self._config.chunk_size
        return pieces

    # -- phase 2: merge with overlap --------------------------------------

    def _merge_with_overlap(
        self,
        original: str,
        splitlets: list[tuple[str, int, int]],
    ) -> list[Chunk]:
        """Pack splitlets into chunks <= chunk_size tokens with overlap between."""
        if not splitlets:
            return []

        chunks: list[Chunk] = []
        # Track the open chunk by character span; its token count is
        # re-derived from `original[chunk_start:chunk_end]` when needed.
        chunk_start: int | None = None
        chunk_end: int = 0

        for _piece_text, p_start, p_end in splitlets:
            if chunk_start is None:
                chunk_start = p_start
                chunk_end = p_end
                continue

            # Would adding this piece (and everything from chunk_end to
            # p_end of the original) overflow the budget?
            candidate_text = original[chunk_start:p_end]
            if self._token_count(candidate_text) <= self._config.chunk_size:
                chunk_end = p_end
                continue

            # Emit the current chunk and start a new one with overlap.
            chunks.append(self._make_chunk(original, len(chunks), chunk_start, chunk_end))
            overlap_start = self._overlap_start(original, chunk_start, chunk_end)
            # If the new piece alone is too big for chunk_size, the next
            # iteration's _make_chunk will still emit it; the overlap
            # contribution is dropped to avoid an infinite loop.
            chunk_start = overlap_start
            chunk_end = p_end
            if self._token_count(original[chunk_start:chunk_end]) > self._config.chunk_size:
                chunk_start = p_start
                chunk_end = p_end

        if chunk_start is not None:
            chunks.append(self._make_chunk(original, len(chunks), chunk_start, chunk_end))

        return chunks

    def _make_chunk(self, original: str, index: int, start: int, end: int) -> Chunk:
        return Chunk(
            chunk_index=index,
            text=original[start:end],
            char_start=start,
            char_end=end,
        )

    def _overlap_start(self, original: str, chunk_start: int, chunk_end: int) -> int:
        """Compute where the next chunk should start to provide token overlap.

        Decodes the last ``overlap`` tokens of the just-finished chunk and
        finds where that text appears at the end of the chunk in the
        original text. Returns the resulting char offset.

        Falls back to a character-count approximation if the tokenizer's
        round-trip doesn't line up exactly.
        """
        if self._config.overlap == 0:
            return chunk_end

        chunk_text = original[chunk_start:chunk_end]
        chunk_tokens = self._enc.encode(chunk_text)
        if len(chunk_tokens) <= self._config.overlap:
            return chunk_start
        overlap_tokens = chunk_tokens[-self._config.overlap :]
        overlap_text = self._enc.decode(overlap_tokens)
        # rfind in the original, bounded to the chunk's span.
        idx = original.rfind(overlap_text, chunk_start, chunk_end)
        if idx >= 0:
            return idx
        # Fallback: approximate. ~4 chars per token is the rough average.
        approx = chunk_end - self._config.overlap * 4
        return max(chunk_start, approx)

    # -- helpers ----------------------------------------------------------

    def _token_count(self, text: str) -> int:
        return len(self._enc.encode(text))
