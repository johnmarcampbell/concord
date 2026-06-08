"""Tests for the JSONL storage backend."""

from datetime import UTC, datetime
from pathlib import Path

from concord.models.proceedings import Article, Issue, Proceeding
from concord.storage.base import Storage
from concord.storage.jsonl import JsonlStorage

DEFAULT_GRANULE = "CREC-2026-05-22-pt1-PgD551-6"


def _sample_proceeding(*, granule_id: str = DEFAULT_GRANULE, text: str = "body") -> Proceeding:
    """Build a Proceeding whose URLs are derived from the granule ID.

    The Article model verifies that the explicit granule_id matches the
    granule embedded in both text_url and pdf_url, so the URLs have to be
    constructed from the same granule ID.
    """
    text_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{granule_id}.htm"
    pdf_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/{granule_id}.pdf"
    issue = Issue(
        issue_date="2026-05-22",
        congress=119,
        session=2,
        volume=172,
        issue_number=88,
        update_date="2026-05-23T06:44:22Z",
    )
    article = Article(
        section="Daily Digest",
        title="Sample",
        start_page="D551",
        end_page="D552",
        text_url=text_url,
        pdf_url=pdf_url,
        granule_id=granule_id,
    )
    return Proceeding.build(
        issue=issue,
        article=article,
        text=text,
        fetched_at=datetime(2026, 5, 24, tzinfo=UTC),
    )


# -- protocol conformance ------------------------------------------------------


class TestProtocol:
    def test_jsonl_storage_satisfies_storage_protocol(self, tmp_path: Path) -> None:
        # This is a runtime check at instance level — Protocol with @runtime_checkable
        # would be needed for isinstance(), but the type signature alone is what
        # callers depend on. Keeping it as a structural assertion.
        storage: Storage = JsonlStorage(tmp_path / "out.jsonl")
        assert hasattr(storage, "has")
        assert hasattr(storage, "write")


# -- basic write / has ---------------------------------------------------------


class TestWriteAndHas:
    def test_has_returns_false_for_unseen(self, tmp_path: Path) -> None:
        storage = JsonlStorage(tmp_path / "out.jsonl")
        assert storage.has("CREC-never-seen") is False

    def test_has_returns_true_after_write(self, tmp_path: Path) -> None:
        storage = JsonlStorage(tmp_path / "out.jsonl")
        p = _sample_proceeding()
        storage.write(p)
        assert storage.has(p.granule_id) is True

    def test_write_creates_file_and_parents(self, tmp_path: Path) -> None:
        path = tmp_path / "subdir/nested/out.jsonl"
        storage = JsonlStorage(path)
        storage.write(_sample_proceeding())
        assert path.exists()
        assert path.read_text().count("\n") == 1

    def test_len_tracks_written_count(self, tmp_path: Path) -> None:
        storage = JsonlStorage(tmp_path / "out.jsonl")
        assert len(storage) == 0
        storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2"))
        assert len(storage) == 2


# -- dedup --------------------------------------------------------------------


class TestDedup:
    def test_writing_same_granule_twice_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        storage = JsonlStorage(path)
        p = _sample_proceeding()
        storage.write(p)
        storage.write(p)  # idempotent
        assert path.read_text().count("\n") == 1
        assert len(storage) == 1

    def test_dedup_persists_across_instances(self, tmp_path: Path) -> None:
        """Re-opening the same file should rebuild the seen-set from disk.

        This is the resume contract that #21 (orchestrator) depends on.
        """
        path = tmp_path / "out.jsonl"
        first = JsonlStorage(path)
        first.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        first.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2"))

        second = JsonlStorage(path)
        assert second.has("CREC-2026-05-22-pt1-PgD551-1")
        assert second.has("CREC-2026-05-22-pt1-PgD551-2")
        assert not second.has("CREC-2026-05-22-pt1-PgD551-3")

        # Writing an already-stored granule via the second instance is a no-op.
        second.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        assert path.read_text().count("\n") == 2


# -- round-trip integrity ------------------------------------------------------


class TestRoundTrip:
    def test_written_proceeding_can_be_read_back(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        original = _sample_proceeding(text="some content body")
        JsonlStorage(path).write(original)

        line = path.read_text().strip()
        roundtripped = Proceeding.model_validate_json(line)
        assert roundtripped == original


# -- malformed-line recovery --------------------------------------------------


class TestMalformedLineRecovery:
    def test_partial_write_line_is_skipped(self, tmp_path: Path) -> None:
        """A crash mid-write leaves a half-line; load shouldn't choke."""
        path = tmp_path / "out.jsonl"
        good = _sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1")
        another_good = _sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2")
        # Hand-author the file: one good line, then a truncated record, then
        # another good line. Simulates a crash in the middle.
        path.write_text(
            good.model_dump_json()
            + "\n"
            + '{"granule_id": "CREC-truncated", "text": "oops no closing\n'  # partial JSON
            + another_good.model_dump_json()
            + "\n",
            encoding="utf-8",
        )

        storage = JsonlStorage(path)
        assert storage.has(good.granule_id)
        assert storage.has(another_good.granule_id)
        # Truncated record didn't poison the load.
        assert not storage.has("CREC-truncated")

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        good = _sample_proceeding()
        path.write_text("\n\n" + good.model_dump_json() + "\n\n", encoding="utf-8")
        storage = JsonlStorage(path)
        assert storage.has(good.granule_id)
        assert len(storage) == 1

    def test_missing_granule_id_field_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        # Valid JSON but missing the field we key on.
        path.write_text('{"text": "no granule_id here"}\n', encoding="utf-8")
        storage = JsonlStorage(path)
        assert len(storage) == 0


# -- empty / missing file ------------------------------------------------------


class TestEmptyFile:
    def test_missing_file_treated_as_empty(self, tmp_path: Path) -> None:
        storage = JsonlStorage(tmp_path / "does-not-exist.jsonl")
        assert len(storage) == 0
        assert not storage.has("anything")

    def test_existing_empty_file_treated_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.touch()
        storage = JsonlStorage(path)
        assert len(storage) == 0

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """Convenience: pass a str instead of Path."""
        storage = JsonlStorage(str(tmp_path / "out.jsonl"))
        storage.write(_sample_proceeding())
        assert len(storage) == 1
