"""Tests for the optional MongoDB storage backend.

Uses ``mongomock`` (a pymongo-compatible in-memory fake) so the suite runs
without a real Mongo server.
"""

from datetime import UTC, datetime

import mongomock

from concord.models import Article, Issue, Proceeding
from concord.storage import MongoStorage, Storage

DEFAULT_GRANULE = "CREC-2026-05-22-pt1-PgD551-6"


def _sample_proceeding(*, granule_id: str = DEFAULT_GRANULE, text: str = "body") -> Proceeding:
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


def _fresh_collection():
    """A fresh mongomock collection per test (mongomock state is per-client)."""
    client = mongomock.MongoClient()
    return client["concord_test"]["proceedings"]


# -- protocol conformance ----------------------------------------------------


class TestProtocol:
    def test_mongo_storage_satisfies_storage_protocol(self) -> None:
        storage: Storage = MongoStorage(collection=_fresh_collection())
        assert hasattr(storage, "has")
        assert hasattr(storage, "write")


class TestIndex:
    def test_construction_creates_unique_granule_id_index(self) -> None:
        col = _fresh_collection()
        MongoStorage(collection=col)
        indexes = col.index_information()
        # granule_id_1 is the auto-generated index name for ascending granule_id.
        assert any(
            "granule_id" in [field for field, _ in info.get("key", [])]
            and info.get("unique") is True
            for info in indexes.values()
        )


# -- basic write / has -------------------------------------------------------


class TestWriteAndHas:
    def test_has_returns_false_for_unseen(self) -> None:
        storage = MongoStorage(collection=_fresh_collection())
        assert storage.has("CREC-never-seen") is False

    def test_has_returns_true_after_write(self) -> None:
        storage = MongoStorage(collection=_fresh_collection())
        p = _sample_proceeding()
        storage.write(p)
        assert storage.has(p.granule_id) is True

    def test_multiple_writes_each_persist(self) -> None:
        col = _fresh_collection()
        storage = MongoStorage(collection=col)
        storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2"))
        assert col.count_documents({}) == 2


# -- dedup -------------------------------------------------------------------


class TestDedup:
    def test_writing_same_granule_twice_is_noop(self) -> None:
        col = _fresh_collection()
        storage = MongoStorage(collection=col)
        p = _sample_proceeding()
        storage.write(p)
        storage.write(p)  # duplicate key error → swallowed
        assert col.count_documents({}) == 1

    def test_dedup_persists_across_instances(self) -> None:
        """Two MongoStorage instances over the same collection share dedup state.

        Unlike JsonlStorage's in-memory set, dedup here is enforced by the
        unique index — there's no caching, so this test just confirms the
        new instance sees the existing document.
        """
        col = _fresh_collection()
        first = MongoStorage(collection=col)
        first.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))

        second = MongoStorage(collection=col)
        assert second.has("CREC-2026-05-22-pt1-PgD551-1")
        assert not second.has("CREC-2026-05-22-pt1-PgD551-2")

        # Writing an already-stored granule from the second instance is a no-op.
        second.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        assert col.count_documents({}) == 1


# -- round-trip integrity -----------------------------------------------------


class TestRoundTrip:
    def test_written_document_can_be_parsed_back_as_proceeding(self) -> None:
        col = _fresh_collection()
        original = _sample_proceeding(text="some content body")
        MongoStorage(collection=col).write(original)

        doc = col.find_one({"granule_id": original.granule_id})
        assert doc is not None
        # Drop the auto-added _id so model_validate sees only Proceeding fields.
        doc.pop("_id", None)
        roundtripped = Proceeding.model_validate(doc)
        assert roundtripped == original


# -- non-duplicate errors propagate -------------------------------------------


class TestErrorPropagation:
    def test_unrelated_exceptions_are_not_swallowed(self) -> None:
        """Only DuplicateKeyError is silenced; other errors must surface."""

        class _BrokenCollection:
            def create_index(self, *_args, **_kwargs):
                return None

            def find_one(self, *_args, **_kwargs):
                return None

            def insert_one(self, *_args, **_kwargs):
                raise RuntimeError("disk full")

        storage = MongoStorage(collection=_BrokenCollection())
        try:
            storage.write(_sample_proceeding())
        except RuntimeError as exc:
            assert "disk full" in str(exc)
        else:
            raise AssertionError("expected RuntimeError to propagate")
