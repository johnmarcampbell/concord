"""Tests for the Pydantic models.

These cover the coercions the models perform on raw API payloads
(date/datetime strings, integer-typed strings) and the granule-ID derivation
logic that ties Article URLs together.
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from concord.models import Article, Issue, Proceeding, parse_granule_id

# Realistic sample URLs captured from the api.congress.gov spike on 2026-05-22.
SAMPLE_TEXT_URL = (
    "https://www.congress.gov/119/crec/2026/05/22/172/88/modified/CREC-2026-05-22-pt1-PgD551-6.htm"
)
SAMPLE_PDF_URL = (
    "https://www.congress.gov/119/crec/2026/05/22/172/88/CREC-2026-05-22-pt1-PgD551-6.pdf"
)
SAMPLE_GRANULE = "CREC-2026-05-22-pt1-PgD551-6"


# -- parse_granule_id ---------------------------------------------------------


class TestParseGranuleId:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (SAMPLE_TEXT_URL, SAMPLE_GRANULE),
            (SAMPLE_PDF_URL, SAMPLE_GRANULE),
            # Senate section, House section, Extensions all share the prefix.
            (
                "https://www.congress.gov/119/crec/2026/05/22/172/88/modified/"
                "CREC-2026-05-22-pt1-PgS1234.htm",
                "CREC-2026-05-22-pt1-PgS1234",
            ),
            (
                "https://www.congress.gov/119/crec/2026/05/22/172/88/"
                "CREC-2026-05-22-pt1-PgH4567-2.pdf",
                "CREC-2026-05-22-pt1-PgH4567-2",
            ),
            # Extensionless form (defensive — current API always has .htm/.pdf).
            (
                "https://example.com/path/CREC-2025-01-15-pt2-PgE99",
                "CREC-2025-01-15-pt2-PgE99",
            ),
        ],
    )
    def test_extracts_granule_id(self, url: str, expected: str) -> None:
        assert parse_granule_id(url) == expected

    def test_rejects_url_without_granule(self) -> None:
        with pytest.raises(ValueError, match="no granule ID"):
            parse_granule_id("https://example.com/not-a-record.htm")


# -- Issue --------------------------------------------------------------------


class TestIssue:
    def test_parses_api_payload(self) -> None:
        # Shape lifted verbatim from a real /v3/daily-congressional-record response.
        payload = {
            "congress": 119,
            "issueDate": "2026-05-22T04:00:00Z",
            "issueNumber": "88",
            "sessionNumber": 2,
            "updateDate": "2026-05-23T06:44:22Z",
            "url": "https://api.congress.gov/v3/daily-congressional-record/172/88?format=json",
            "volumeNumber": 172,
        }
        # The API uses camelCase; show that callers map to snake_case explicitly.
        issue = Issue(
            issue_date=payload["issueDate"],
            congress=payload["congress"],
            session=payload["sessionNumber"],
            volume=payload["volumeNumber"],
            issue_number=payload["issueNumber"],
            update_date=payload["updateDate"],
        )
        assert issue.issue_date == date(2026, 5, 22)
        assert issue.issue_number == 88
        assert issue.session == 2
        assert issue.update_date == datetime(2026, 5, 23, 6, 44, 22, tzinfo=UTC)

    def test_session_must_be_one_or_two(self) -> None:
        with pytest.raises(ValidationError):
            Issue(
                issue_date="2026-05-22",
                congress=119,
                session=3,  # type: ignore[arg-type]
                volume=172,
                issue_number=88,
                update_date="2026-05-23T06:44:22Z",
            )

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Issue(
                issue_date="2026-05-22",
                congress=119,
                session=2,
                volume=172,
                # issue_number missing
                update_date="2026-05-23T06:44:22Z",
            )  # type: ignore[call-arg]
        assert "issue_number" in str(exc.value)

    def test_accepts_plain_date_string(self) -> None:
        issue = Issue(
            issue_date="2026-05-22",
            congress=119,
            session=2,
            volume=172,
            issue_number=88,
            update_date="2026-05-23T06:44:22Z",
        )
        assert issue.issue_date == date(2026, 5, 22)


# -- Article ------------------------------------------------------------------


class TestArticle:
    def _article(self, **overrides: object) -> Article:
        defaults: dict[str, object] = {
            "section": "Daily Digest",
            "title": "Daily Digest/Next Meeting of the SENATE",
            "start_page": "D551",
            "end_page": "D552",
            "text_url": SAMPLE_TEXT_URL,
            "pdf_url": SAMPLE_PDF_URL,
        }
        defaults.update(overrides)
        return Article(**defaults)  # type: ignore[arg-type]

    def test_derives_granule_id_from_text_url(self) -> None:
        article = self._article()
        assert article.granule_id == SAMPLE_GRANULE

    def test_explicit_granule_id_is_honored_when_consistent(self) -> None:
        article = self._article(granule_id=SAMPLE_GRANULE)
        assert article.granule_id == SAMPLE_GRANULE

    def test_inconsistent_explicit_granule_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            self._article(granule_id="CREC-1999-01-01-pt1-PgS1")

    def test_mismatched_pdf_and_text_urls_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            self._article(
                pdf_url=(
                    "https://www.congress.gov/119/crec/2026/05/22/172/88/"
                    "CREC-2026-05-22-pt1-PgD999-1.pdf"
                ),
            )

    def test_malformed_url_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._article(text_url="not-a-url")

    def test_url_without_granule_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._article(text_url="https://example.com/no-granule-here.htm")


# -- Proceeding ---------------------------------------------------------------


class TestProceeding:
    def test_build_flattens_issue_and_article(self) -> None:
        issue = Issue(
            issue_date="2026-05-22",
            congress=119,
            session=2,
            volume=172,
            issue_number=88,
            update_date="2026-05-23T06:44:22Z",
        )
        article = Article(
            section="Senate",
            title="EXAMPLE PROCEEDING",
            start_page="S1234",
            end_page="S1235",
            text_url=SAMPLE_TEXT_URL,
            pdf_url=SAMPLE_PDF_URL,
        )  # type: ignore[call-arg]
        fetched_at = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)

        proceeding = Proceeding.build(
            issue=issue, article=article, text="hello world", fetched_at=fetched_at
        )

        assert proceeding.granule_id == SAMPLE_GRANULE
        assert proceeding.issue_date == date(2026, 5, 22)
        assert proceeding.title == "EXAMPLE PROCEEDING"
        assert proceeding.text == "hello world"
        assert proceeding.fetched_at == fetched_at

    def test_round_trips_through_json(self) -> None:
        """Serializing a Proceeding and reloading produces an equal object.

        This is the contract the JSONL storage backend (#20) relies on.
        """
        issue = Issue(
            issue_date="2026-05-22",
            congress=119,
            session=2,
            volume=172,
            issue_number=88,
            update_date="2026-05-23T06:44:22Z",
        )
        article = Article(
            section="House",
            title="X",
            start_page="H1",
            end_page="H1",
            text_url=SAMPLE_TEXT_URL,
            pdf_url=SAMPLE_PDF_URL,
        )  # type: ignore[call-arg]
        original = Proceeding.build(
            issue=issue,
            article=article,
            text="body",
            fetched_at=datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
        )

        roundtripped = Proceeding.model_validate_json(original.model_dump_json())
        assert roundtripped == original
