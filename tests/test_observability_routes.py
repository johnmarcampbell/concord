"""Endpoint-bucket route table — every api.congress.gov path shape maps to a
stable bucket, and an unknown path falls to ``api:unmatched`` (ADR 0021)."""

from datetime import UTC, datetime

import pytest

from concord.observability import Recorder, normalize


class TestNormalize:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # daily congressional record — list + articles
            ("/daily-congressional-record", "api:daily-record/list"),
            ("/daily-congressional-record/", "api:daily-record/list"),
            ("/daily-congressional-record/172/88/articles", "api:daily-record/articles"),
            # members
            ("/member/congress/119", "api:member/list"),
            # bills — list, detail, and each sub-endpoint
            ("/bill/119/hr", "api:bill/list"),
            ("/bill/119/hr/1234", "api:bill/detail"),
            ("/bill/119/hr/1234/cosponsors", "api:bill/cosponsors"),
            ("/bill/119/hjres/22/actions", "api:bill/actions"),
            ("/bill/118/sconres/3/subjects", "api:bill/subjects"),
            ("/bill/119/s/5/titles", "api:bill/titles"),
            ("/bill/119/hr/1/summaries", "api:bill/summaries"),
            # house votes — list, detail, members
            ("/house-vote/119/1", "api:house-vote/list"),
            ("/house-vote/119/1/42", "api:house-vote/detail"),
            ("/house-vote/119/1/42/members", "api:house-vote/members"),
        ],
    )
    def test_known_api_paths_map_to_buckets(self, path: str, expected: str) -> None:
        bucket, matched = normalize("api", path)
        assert bucket == expected
        assert matched is True

    def test_trailing_slash_does_not_change_bucket(self) -> None:
        assert normalize("api", "/bill/119/hr/1234/") == ("api:bill/detail", True)

    @pytest.mark.parametrize(
        "path",
        [
            "/totally-unknown-endpoint",
            "/bill",
            "/bill/119",
            "/member",
            "/house-vote/119",  # one numeric segment short of the list shape
        ],
    )
    def test_unknown_path_falls_to_unmatched(self, path: str) -> None:
        assert normalize("api", path) == ("api:unmatched", False)

    def test_unknown_source_is_unmatched(self) -> None:
        assert normalize("text", "/anything") == ("text:unmatched", False)
        assert normalize("senate", "/anything") == ("senate:unmatched", False)


class TestUnmatchedSampling:
    def _recorder(self) -> Recorder:
        return Recorder(entity="bills", command="scrape bills", started_at=datetime.now(UTC))

    def test_unmatched_path_is_sampled_on_success(self) -> None:
        rec = self._recorder()
        rec.note_success("api", "/brand-new-endpoint/42")
        assert rec.successes == {"api:unmatched": 1}
        assert "/brand-new-endpoint/42" in rec.unmatched

    def test_unmatched_sample_deduplicates(self) -> None:
        rec = self._recorder()
        rec.note_success("api", "/x/1")
        rec.note_success("api", "/x/1")
        assert rec.successes == {"api:unmatched": 2}
        assert rec.unmatched == {"/x/1"}

    def test_unmatched_sample_is_capped(self) -> None:
        rec = self._recorder()
        for i in range(50):
            rec.note_success("api", f"/unknown/{i}")
        assert len(rec.unmatched) <= 20
        # All 50 still counted into the bucket, even though only 20 are sampled.
        assert rec.successes == {"api:unmatched": 50}
