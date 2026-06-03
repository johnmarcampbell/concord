"""Unit tests for cross-cutting helpers in :mod:`concord._common`."""

import pytest

from concord._common import ordinal


class TestOrdinal:
    """English ordinal suffixes, with the 11-13 special case."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            # Single-digit base cases: 1/2/3 get st/nd/rd, the rest th.
            (0, "0th"),
            (1, "1st"),
            (2, "2nd"),
            (3, "3rd"),
            (4, "4th"),
            (5, "5th"),
            (9, "9th"),
            (10, "10th"),
            # The 11-13 special case: these take th despite ending in 1/2/3.
            (11, "11th"),
            (12, "12th"),
            (13, "13th"),
            (14, "14th"),
            # Past the teens, the last digit rules again: 21st/22nd/23rd.
            (20, "20th"),
            (21, "21st"),
            (22, "22nd"),
            (23, "23rd"),
            (24, "24th"),
            # The current corpus and the first future breaks (issue #98).
            (119, "119th"),
            (121, "121st"),
            (122, "122nd"),
            (123, "123rd"),
            # The 11-13 rule keys off the last *two* digits, so 111-113
            # are th even though they end in 1/2/3.
            (100, "100th"),
            (101, "101st"),
            (102, "102nd"),
            (103, "103rd"),
            (111, "111th"),
            (112, "112th"),
            (113, "113th"),
            (211, "211th"),
            (221, "221st"),
        ],
    )
    def test_ordinal(self, n: int, expected: str) -> None:
        assert ordinal(n) == expected
