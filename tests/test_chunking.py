"""Tests for the recursive token-aware text splitter."""

from itertools import pairwise

import pytest

from concord.chunking import Chunk, Chunker, ChunkerConfig


@pytest.fixture
def small_chunker() -> Chunker:
    """Chunker with tiny limits so tests can force splits with short inputs."""
    return Chunker(ChunkerConfig(chunk_size=30, overlap=8))


@pytest.fixture
def default_chunker() -> Chunker:
    """Production-config chunker (512 / 100)."""
    return Chunker()


# -- config validation --------------------------------------------------------


class TestChunkerConfig:
    def test_default_config_matches_adr_0005(self) -> None:
        c = ChunkerConfig()
        assert c.chunk_size == 512
        assert c.overlap == 100
        assert c.encoding_name == "cl100k_base"

    def test_overlap_greater_than_chunk_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="overlap must be less than"):
            Chunker(ChunkerConfig(chunk_size=100, overlap=100))

    def test_zero_chunk_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            Chunker(ChunkerConfig(chunk_size=0, overlap=0))

    def test_negative_overlap_rejected(self) -> None:
        with pytest.raises(ValueError, match="overlap must be non-negative"):
            Chunker(ChunkerConfig(chunk_size=100, overlap=-1))


# -- basic chunking behavior --------------------------------------------------


class TestChunkSplitting:
    def test_short_text_one_chunk(self, default_chunker: Chunker) -> None:
        text = "A short sentence under the chunk size limit."
        chunks = default_chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].chunk_index == 0
        assert chunks[0].char_start == 0
        assert chunks[0].char_end == len(text)

    def test_empty_text_returns_no_chunks(self, default_chunker: Chunker) -> None:
        assert default_chunker.chunk("") == []

    def test_whitespace_only_text_returns_no_chunks(self, default_chunker: Chunker) -> None:
        assert default_chunker.chunk("   \n\n  \t  ") == []

    def test_long_text_produces_multiple_chunks(self, small_chunker: Chunker) -> None:
        text = " ".join([f"word{i}" for i in range(200)])
        chunks = small_chunker.chunk(text)
        assert len(chunks) > 1
        # Indices are sequential starting from zero.
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_chunk_indices_are_sequential(self, small_chunker: Chunker) -> None:
        text = "Paragraph one.\n\n" * 30
        chunks = small_chunker.chunk(text)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


# -- boundary preferences -----------------------------------------------------


class TestBoundaryPreferences:
    def test_prefers_paragraph_boundaries(self, small_chunker: Chunker) -> None:
        # Three paragraphs, each below chunk_size when alone but oversize
        # combined. The splitter should split on \n\n, not mid-paragraph.
        paragraphs = [
            "Senator A spoke at length about banking regulation in the morning.",
            "Senator B raised concerns over the proposed amendments to bill H R 1234.",
            "The chamber voted to recess until the following day.",
        ]
        text = "\n\n".join(paragraphs)
        chunks = small_chunker.chunk(text)
        # Each chunk's text, stripped, should align with paragraph boundaries
        # (one or more paragraphs concatenated). Sloppy check: each chunk
        # contains at least one full paragraph from the input.
        for chunk in chunks:
            assert any(p in chunk.text for p in paragraphs), (
                f"chunk {chunk.text!r} doesn't contain any full paragraph"
            )

    def test_falls_back_to_hard_split_for_one_giant_word(self, small_chunker: Chunker) -> None:
        # One giant blob with no whitespace at all. Recursion exhausts all
        # separators and falls into the hard token split.
        giant = "x" * 2000
        chunks = small_chunker.chunk(giant)
        # Must produce multiple chunks (hard split kicked in).
        assert len(chunks) > 1
        # No chunk exceeds the token budget.
        for chunk in chunks:
            assert small_chunker._token_count(chunk.text) <= small_chunker.config.chunk_size


# -- offsets ------------------------------------------------------------------


class TestCharOffsets:
    def test_offsets_round_trip_on_short_text(self, default_chunker: Chunker) -> None:
        text = "The Senate convened at 10am today."
        for chunk in default_chunker.chunk(text):
            assert text[chunk.char_start : chunk.char_end] == chunk.text

    def test_offsets_round_trip_on_multi_chunk_text(self, small_chunker: Chunker) -> None:
        text = (
            "First paragraph here, just to take up some space.\n\n"
            "Second paragraph follows, with a bit more substance.\n\n"
            "Third paragraph wraps everything up.\n\n"
            "And one final paragraph for good measure."
        )
        for chunk in small_chunker.chunk(text):
            actual = text[chunk.char_start : chunk.char_end]
            assert actual == chunk.text, f"offset mismatch: stored={chunk.text!r} actual={actual!r}"

    def test_first_chunk_starts_at_zero_when_text_starts_with_content(
        self, small_chunker: Chunker
    ) -> None:
        text = "Word " * 50
        chunks = small_chunker.chunk(text)
        assert chunks[0].char_start == 0

    def test_last_chunk_ends_near_text_length(self, small_chunker: Chunker) -> None:
        # Trailing whitespace may be excluded from the last chunk's span
        # (the separator-split pipeline drops empty trailing pieces). The
        # important property is that the last chunk reaches the end of the
        # *content*, not necessarily the very last byte.
        text = ("Word " * 50).rstrip()
        chunks = small_chunker.chunk(text)
        assert chunks[-1].char_end == len(text)


# -- overlap ------------------------------------------------------------------


class TestOverlap:
    def test_consecutive_chunks_overlap_when_overlap_is_nonzero(self) -> None:
        chunker = Chunker(ChunkerConfig(chunk_size=30, overlap=8))
        text = (
            "The Senate convened. Members deliberated all morning. "
            "An amendment was proposed. Debate continued through the afternoon. "
            "A vote was scheduled. Adjournment came at five. "
            "The next session was set. Various committees met. "
            "Reports were filed. Comments were noted."
        )
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2, "test expects multi-chunk output"
        # Consecutive chunks should share *some* common tail/head text.
        for a, b in pairwise(chunks):
            # Sloppy check: the first 5 chars of b appear somewhere in the
            # last 50 chars of a. Tokenization makes exact boundary tests
            # brittle, so this is intentionally loose.
            tail = a.text[-50:]
            head = b.text[:5]
            assert head in tail, f"no apparent overlap between {a.text[-30:]!r} and {b.text[:30]!r}"

    def test_no_overlap_when_overlap_is_zero(self) -> None:
        chunker = Chunker(ChunkerConfig(chunk_size=30, overlap=0))
        text = " ".join(f"word{i}" for i in range(200))
        chunks = chunker.chunk(text)
        # Total characters in chunks == characters in original (no duplication).
        # Allow small slack for separator whitespace at boundaries; the
        # important property is that overlap is *zero*, not positive.
        joined = "".join(c.text for c in chunks)
        assert len(joined) <= len(text) + len(chunks)


# -- token budget enforcement -------------------------------------------------


class TestTokenBudget:
    def test_no_chunk_exceeds_chunk_size(self) -> None:
        chunker = Chunker(ChunkerConfig(chunk_size=40, overlap=10))
        text = (
            "Article one talks about banking policy at length. " * 50
            + "Article two talks about appropriations. " * 50
        )
        chunks = chunker.chunk(text)
        for chunk in chunks:
            assert chunker._token_count(chunk.text) <= chunker.config.chunk_size, (
                f"chunk {chunk.chunk_index} has {chunker._token_count(chunk.text)} tokens"
            )


# -- types ----------------------------------------------------------------


class TestTypes:
    def test_chunks_are_pydantic_models(self, default_chunker: Chunker) -> None:
        text = "Hello world."
        [chunk] = default_chunker.chunk(text)
        assert isinstance(chunk, Chunk)
        # Pydantic round-trip
        dumped = chunk.model_dump()
        assert Chunk.model_validate(dumped) == chunk
